"""
Shared test setup. Every test gets a fresh SQLite database copied from a
template that was built once per run by applying the real Alembic
migrations — so the migrations themselves are exercised on every run, and
a test can never see another test's rows.

Environment is pinned BEFORE the app is imported: app.py reads its config
at import time, and load_dotenv() never overrides variables that are
already set, so a developer's own .env (real SMTP/Google/KACE creds) can't
leak into a test run.
"""
import os
import shutil
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix='foxdesk-tests-')
DB_PATH = os.path.join(_TMP, 'test.db')
TEMPLATE_PATH = os.path.join(_TMP, 'template.db')
ADMIN_PASSWORD = 'test-admin-password'

os.environ.update({
    'DATABASE_URL': f'sqlite:///{DB_PATH}',
    'FLASK_ENV': 'development',
    'ADMIN_PASSWORD': ADMIN_PASSWORD,
    'SECRET_KEY': 'test-secret',
    'SMTP_FROM_EMAIL': '', 'SMTP_USERNAME': '', 'SMTP_PASSWORD': '',
    'GOOGLE_SERVICE_ACCOUNT_FILE': '', 'GOOGLE_ADMIN_IMPERSONATE_EMAIL': '',
    'KACE_URL': '', 'KACE_USERNAME': '', 'KACE_PASSWORD': '',
    'AD_SERVERS': '', 'AD_BASE_DN': '', 'AD_BIND_USER': '', 'AD_BIND_PASSWORD': '', 'AD_CA_FILE': '',
    'DELL_CLIENT_ID': '', 'DELL_CLIENT_SECRET': '', 'APP_URL': '', 'APP_TIMEZONE': 'America/New_York',
    'GOOGLE_OAUTH_CLIENT_ID': '', 'GOOGLE_OAUTH_CLIENT_SECRET': '', 'ALLOW_SHARED_PASSWORD': '',
})
sys.path.insert(0, ROOT)

import foxdesk  # noqa: E402,F401  (must come after the environment is pinned)
from flask_migrate import upgrade  # noqa: E402


def _foxdesk_modules():
    return [m for name, m in sorted(sys.modules.items()) if name.startswith('foxdesk') and m is not None]


class _AnyModule:
    """`A.Name` finds Name in whichever foxdesk module defines it, so tests
    don't have to track which module each helper lives in."""
    def __getattr__(self, name):
        for m in _foxdesk_modules():
            if name in vars(m):
                return vars(m)[name]
        raise AttributeError(name)


A = _AnyModule()


def patch_everywhere(monkeypatch, name, value):
    """Replace `name` in every foxdesk module that has it — needed because
    modules import helpers/constants by name (`from ... import send_email`),
    so patching only the defining module wouldn't reach the callers."""
    hits = 0
    for m in _foxdesk_modules():
        if name in vars(m):
            monkeypatch.setattr(m, name, value)
            hits += 1
    assert hits, f'{name} not found in any foxdesk module'

A.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

with A.app.app_context():
    upgrade(directory=os.path.join(ROOT, 'migrations'))
    A.db.session.remove()
    A.db.engine.dispose()
shutil.copy(DB_PATH, TEMPLATE_PATH)


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset to the freshly-migrated template before every test."""
    with A.app.app_context():
        A.db.session.remove()
        A.db.engine.dispose()
    shutil.copy(TEMPLATE_PATH, DB_PATH)
    with A.app.app_context():
        yield A.db
        A.db.session.remove()


@pytest.fixture
def app_module():
    return A


@pytest.fixture
def client():
    """A test client logged in through the real login form (shared admin password)."""
    c = A.app.test_client()
    r = c.post('/admin/login', data={'username': '', 'password': ADMIN_PASSWORD})
    assert r.status_code == 302, 'login failed'
    return c


@pytest.fixture
def anon_client():
    return A.app.test_client()


@pytest.fixture
def sent_emails(monkeypatch):
    """Turns email 'on' and captures every send instead of hitting SMTP.
    Background sends run inline so tests can assert on them immediately."""
    sent = []
    fake_send = lambda to, subject, body: sent.append((to, subject, body))  # noqa: E731
    patch_everywhere(monkeypatch, 'EMAIL_ENABLED', True)
    patch_everywhere(monkeypatch, 'send_email', fake_send)
    patch_everywhere(monkeypatch, '_send_email_in_background', fake_send)
    return sent


# ─── Factories ────────────────────────────────────────────────────────────────

class Factory:
    def __init__(self):
        self._n = 0

    def _next(self):
        self._n += 1
        return self._n

    def site(self, name=None):
        s = A.Site(name=name or f'Test School {self._next()}')
        A.db.session.add(s)
        A.db.session.commit()
        return s

    def person(self, first='Pat', last=None, role='student', site=None, active=True, email=None, **kw):
        n = self._next()
        p = A.Person(first_name=first, last_name=last or f'Tester{n}', role=role, is_active=active,
                     email=email or f'{first.lower()}.{n}@example.edu', site_id=site.id if site else None, **kw)
        A.db.session.add(p)
        A.db.session.commit()
        return p

    def device(self, tag=None, serial=None, holder=None, site=None, status=None, device_type='chromebook',
               assigned_days_ago=20, **asset_fields):
        n = self._next()
        tag = tag or f'T{n:05d}'
        row = A.AssetRegistry(asset_tag=tag, serial_number=serial if serial is not None else f'SN{n:06d}',
                              device_type=device_type, site_id=site.id if site else None)
        A.db.session.add(row)
        asset = A.Asset(asset_tag=tag, is_valid=True, status=status or ('assigned' if holder else 'available'),
                        assigned_to_id=holder.id if holder else None, **asset_fields)
        A.db.session.add(asset)
        if holder:
            A.db.session.add(A.AssignmentHistory(asset_tag=tag, person_id=holder.id, person_name=holder.full_name,
                                                 assigned_at=datetime.utcnow() - timedelta(days=assigned_days_ago)))
        A.db.session.commit()
        return row

    def ticket_category(self, name='Hardware', price=None):
        c = A.TicketCategory(name=name, default_price=price)
        A.db.session.add(c)
        A.db.session.commit()
        return c


@pytest.fixture
def make():
    return Factory()


def png_bytes():
    """The smallest valid PNG (1x1) — enough to pass magic-byte sniffing."""
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
