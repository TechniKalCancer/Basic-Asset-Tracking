"""Active Directory: read users and computers over LDAPS and attach them to
the people and devices FoxDesk already has (see services/identities).

Read-only. The bind account is a plain Domain Users member, the connection
is opened read-only, and nothing is ever written to AD.

Settings: the connection (DCs, search base, account, password, optional CA
certificate) is entered on the Active Directory page and saved on
DirectorySettings, the password encrypted with a key derived from
SECRET_KEY. Anything left blank there falls back to the AD_* environment
variables, so an install configured through .env keeps working.

Connecting: each DC is tried in order, and its certificate has to be
trusted before the password is sent. Either it chains to the CA certificate
(saved, or AD_CA_FILE) or a system CA, or an admin trusted that exact
certificate on the page after comparing its thumbprint with the one on the
DC (DirectorySettings.trusted_certs) — the usual case for a school DC with
a self-signed certificate.

Scope: everything under the search base is read (a district is a few thousand
objects), but only objects directly inside a container the admin picked are
placed. An account that's already linked keeps updating after it moves out
of scope — typically into a Disabled OU — so FoxDesk learns it was disabled.
"""
import base64
import hashlib
import re
import socket
import ssl
import uuid
from collections import Counter
from datetime import datetime, timedelta

from flask import has_request_context, request

from foxdesk.core import AD_BASE_DN, AD_BIND_PASSWORD, AD_BIND_USER, AD_CA_FILE, AD_SERVERS, app, db, logger
from foxdesk.models import DeviceRecord, DirectorySettings, PersonIdentity
from foxdesk.services.auth import _log_activity
from foxdesk.services.identities import find_person, find_registry_row, place_account, place_device_record

SOURCE = 'ad'
LDAPS_PORT = 636
STALE_DAYS = 90

USER_ATTRS = ['objectGUID', 'sAMAccountName', 'userPrincipalName', 'mail', 'givenName', 'sn', 'displayName',
              'employeeID', 'employeeNumber', 'title', 'department', 'physicalDeliveryOfficeName',
              'userAccountControl', 'lastLogonTimestamp', 'proxyAddresses']
COMPUTER_ATTRS = ['objectGUID', 'name', 'dNSHostName', 'operatingSystem', 'operatingSystemVersion',
                  'userAccountControl', 'lastLogonTimestamp', 'serialNumber', 'description']


class DirectoryError(Exception):
    """A connection or search problem, already worded for the admin."""


def ad_settings():
    row = DirectorySettings.query.filter_by(source=SOURCE).first()
    if row is None:
        row = DirectorySettings(source=SOURCE)
        db.session.add(row)
        db.session.flush()
    return row


# ─── connection settings ──────────────────────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(('foxdesk-directory-password:' + app.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token):
    """None if it can't be read — usually SECRET_KEY changed since it was saved."""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def split_servers(text):
    """'ldaps://ad1.x.org:636, ad2.x.org' -> ['ad1.x.org', 'ad2.x.org']."""
    hosts = []
    for item in re.split(r'[\s,;]+', text or ''):
        host = re.sub(r'^ldaps?://', '', item.strip(), flags=re.IGNORECASE).split('/')[0].split(':')[0]
        if host and host.lower() not in [h.lower() for h in hosts]:
            hosts.append(host)
    return hosts


def ad_config():
    """The connection settings in effect: saved on the Active Directory page,
    else the AD_* environment variables. `sources` says where each came from
    ('saved' / 'env' / None). Cached per request — feature checks ask often."""
    if has_request_context() and getattr(request, '_ad_config', None) is not None:
        return request._ad_config
    row = DirectorySettings.query.filter_by(source=SOURCE).first()
    sources, cfg = {}, {}

    def pick(key, saved, env):
        sources[key] = 'saved' if saved else ('env' if env else None)
        return saved or env

    cfg['servers'] = pick('servers', split_servers(row.servers) if row and row.servers else [], AD_SERVERS)
    cfg['base_dn'] = pick('base_dn', row.base_dn if row else None, AD_BASE_DN)
    cfg['bind_user'] = pick('bind_user', row.bind_user if row else None, AD_BIND_USER)
    saved_password = decrypt_secret(row.bind_password) if row and row.bind_password else None
    cfg['password_unreadable'] = bool(row and row.bind_password and saved_password is None)
    cfg['password'] = pick('password', saved_password, AD_BIND_PASSWORD)
    cfg['ca_pem'] = (row.ca_pem if row else None) or None
    cfg['ca_file'] = AD_CA_FILE
    cfg['sources'] = sources
    cfg['configured'] = bool(cfg['servers'] and cfg['base_dn'] and cfg['bind_user'] and cfg['password'])
    if has_request_context():
        request._ad_config = cfg
    return cfg


def forget_config_cache():
    if has_request_context():
        request._ad_config = None


def validate_connection_form(servers, base_dn, bind_user, password, ca_pem):
    """Cleaned values, or ValueError with a message for the admin."""
    hosts = split_servers(servers)
    if any(not re.fullmatch(r'[A-Za-z0-9.-]+', h) for h in hosts):
        raise ValueError('Domain controllers should be host names like ad1.yourdistrict.org, separated by commas.')
    base_dn = (base_dn or '').strip()
    if base_dn and 'dc=' not in base_dn.lower():
        raise ValueError('The search base should look like DC=yourdistrict,DC=org.')
    bind_user = (bind_user or '').strip()
    if bind_user and '@' not in bind_user and '\\' not in bind_user and '=' not in bind_user:
        raise ValueError('Enter the account as svc-foxdesk@yourdistrict.org (or DOMAIN\\svc-foxdesk).')
    if password and len(password) > 256:
        raise ValueError('That password is too long.')
    ca_pem = (ca_pem or '').strip() or None
    if ca_pem:
        if '-----BEGIN CERTIFICATE-----' not in ca_pem:
            raise ValueError('Paste the CA certificate in PEM form (it starts with -----BEGIN CERTIFICATE-----).')
        try:
            ssl.create_default_context(cadata=ca_pem)
        except (ssl.SSLError, ValueError):
            raise ValueError('That CA certificate couldn\'t be read.')
    return hosts, base_dn or None, bind_user or None, password or None, ca_pem


# ─── DNs ──────────────────────────────────────────────────────────────────────

_RDN_SPLIT = re.compile(r'(?<!\\),')


def dn_rdns(dn):
    return [r.strip() for r in _RDN_SPLIT.split(dn or '') if r.strip()]


def dn_parent(dn):
    return ','.join(dn_rdns(dn)[1:])


def short_dn(dn, base=None):
    """The DN without the domain part, for display: 'OU=2029' instead of 'OU=2029,DC=fchs,DC=net'."""
    base = (base if base is not None else ad_config()['base_dn'] or '').lower()
    if base and (dn or '').lower().endswith(',' + base):
        return dn[:-(len(base) + 1)]
    return dn


def grad_year_from_dn(dn):
    """Districts commonly keep students in one OU per graduation year."""
    for rdn in dn_rdns(dn)[1:]:
        m = re.fullmatch(r'OU=(20\d\d)', rdn, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def role_from_dn(dn):
    return 'student' if grad_year_from_dn(dn) or 'student' in dn.lower() else 'staff'


# ─── raw LDAP values → plain dicts ───────────────────────────────────────────

def _texts(raw, name):
    return [v.decode('utf-8', 'replace').strip() if isinstance(v, bytes) else str(v).strip()
            for v in raw.get(name.lower()) or []]


def _text(raw, name):
    values = _texts(raw, name)
    return (values[0] or None) if values else None


def _filetime(raw, name):
    """AD's 100-nanosecond intervals since 1601, or None for never/unset."""
    try:
        value = int(_text(raw, name) or 0)
    except ValueError:
        return None
    if value <= 0 or value >= 0x7FFFFFFFFFFFFFFF:
        return None
    return datetime(1601, 1, 1) + timedelta(microseconds=value // 10)


def _guid(raw):
    values = raw.get('objectguid') or []
    value = values[0] if values else None
    return str(uuid.UUID(bytes_le=value)) if isinstance(value, bytes) and len(value) == 16 else None


def _enabled(raw):
    try:
        return not int(_text(raw, 'userAccountControl') or 0) & 2  # ACCOUNTDISABLE
    except ValueError:
        return True


def parse_user(dn, raw):
    raw = {k.lower(): v for k, v in raw.items()}
    mail, upn = _text(raw, 'mail'), _text(raw, 'userPrincipalName')
    aliases = [p[5:].lower() for p in _texts(raw, 'proxyAddresses') if p.lower().startswith('smtp:')]
    return dict(
        guid=_guid(raw), dn=dn, parent=dn_parent(dn).lower(), sam=_text(raw, 'sAMAccountName'),
        upn=upn.lower() if upn else None, mail=mail.lower() if mail else None,
        first=_text(raw, 'givenName'), last=_text(raw, 'sn'), display=_text(raw, 'displayName'),
        employee_id=_text(raw, 'employeeID') or _text(raw, 'employeeNumber'),
        title=_text(raw, 'title'), department=_text(raw, 'department'),
        office=_text(raw, 'physicalDeliveryOfficeName'), enabled=_enabled(raw),
        last_logon=_filetime(raw, 'lastLogonTimestamp'), aliases=aliases,
        role=role_from_dn(dn), grad_year=grad_year_from_dn(dn),
    )


def parse_computer(dn, raw):
    raw = {k.lower(): v for k, v in raw.items()}
    return dict(
        guid=_guid(raw), dn=dn, parent=dn_parent(dn).lower(), name=_text(raw, 'name'),
        dns_name=_text(raw, 'dNSHostName'), os=_text(raw, 'operatingSystem'),
        os_version=_text(raw, 'operatingSystemVersion'), enabled=_enabled(raw),
        last_logon=_filetime(raw, 'lastLogonTimestamp'), serial=_text(raw, 'serialNumber'),
        description=_text(raw, 'description'),
    )


# ─── certificates ─────────────────────────────────────────────────────────────

def describe_certificate(der):
    """Thumbprints plus readable details. sha1 is upper-case hex with no
    separators — exactly what Windows shows as a certificate's Thumbprint."""
    info = dict(sha256=hashlib.sha256(der).hexdigest(), sha1=hashlib.sha1(der).hexdigest().upper(),
                subject=None, issuer=None, not_after=None, names=[], self_signed=None)
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        info['subject'] = cert.subject.rfc4514_string()
        info['issuer'] = cert.issuer.rfc4514_string()
        info['self_signed'] = cert.subject == cert.issuer
        expires = getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after
        info['not_after'] = expires.strftime('%Y-%m-%d')
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            info['names'] = san.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            pass
    except Exception as e:  # details are a convenience; the thumbprints are what matter
        logger.warning('Could not read certificate details: %s', e)
    return info


def _explain(host, error):
    text = str(error)
    lower = text.lower()
    if isinstance(error, (ConnectionResetError, ssl.SSLEOFError)) or 'reset' in lower or 'eof' in lower:
        return (f'{host} answered on port {LDAPS_PORT} but has no LDAPS certificate yet. '
                'Give the DC a certificate for LDAPS, then check again.')
    if 'certificate verify failed' in lower or 'certificate_verify_failed' in lower:
        return f'{host}\'s certificate isn\'t trusted yet. Check it below and trust it if the thumbprint matches the DC.'
    if isinstance(error, socket.timeout) or 'timed out' in lower:
        return f'{host} didn\'t answer on port {LDAPS_PORT} (timed out). Check the firewall between this server and the DC.'
    if isinstance(error, ConnectionRefusedError) or 'refused' in lower:
        return f'{host} refused the connection on port {LDAPS_PORT}.'
    if isinstance(error, socket.gaierror) or 'name or service not known' in lower or 'nodename' in lower:
        return f'{host} couldn\'t be found in DNS.'
    return f'{host}: {text}'


def server_certificate(host, timeout=6):
    """The certificate a DC presents on LDAPS, fetched WITHOUT trusting it,
    so an admin can compare it with the one on the DC."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, LDAPS_PORT), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as e:
        raise DirectoryError(_explain(host, e))
    if not der:
        raise DirectoryError(f'{host} didn\'t present a certificate.')
    return describe_certificate(der)


def verified_by_ca(host, timeout=6):
    """True if the DC's certificate chains to the CA certificate (or a system CA) and names this host."""
    cfg = ad_config()
    try:
        ctx = ssl.create_default_context(cafile=cfg['ca_file'], cadata=cfg['ca_pem'])
        with socket.create_connection((host, LDAPS_PORT), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except (OSError, ssl.SSLError):
        return False


def trust_certificate(settings, host, sha256):
    """Trust the certificate `host` presents now — but only if it is the one
    the admin was shown (sha256), so a swap in between can't sneak in."""
    host = host.lower()
    if host not in [h.lower() for h in ad_config()['servers']]:
        raise DirectoryError(f'{host} isn\'t one of the configured domain controllers.')
    current = server_certificate(host)
    if current['sha256'] != (sha256 or '').lower():
        raise DirectoryError(f'{host} is presenting a different certificate than the one you were shown. '
                             'Check it again before trusting it.')
    settings.trusted_certs = dict(settings.trusted_certs or {}, **{host: current['sha256']})
    return current


# ─── connecting and reading ───────────────────────────────────────────────────

_BIND_ERRORS = {
    '525': 'the account doesn\'t exist', '52e': 'the password is wrong', '530': 'it isn\'t allowed to log on now',
    '531': 'it isn\'t allowed to log on from this computer', '532': 'its password has expired',
    '533': 'the account is disabled', '701': 'the account has expired', '773': 'its password must be reset',
    '775': 'the account is locked out',
}


def _bind_error(result, user):
    message = (result or {}).get('message') or ''
    m = re.search(r'data ([0-9a-f]{3})', message)
    reason = _BIND_ERRORS.get(m.group(1)) if m else None
    return f'login as {user} failed: {reason or (result or {}).get("description") or message}'


def connect(settings, hosts=None):
    """A bound, read-only connection to the first trusted DC that answers.
    Returns (connection, host)."""
    import ldap3
    from ldap3.core.exceptions import LDAPException
    cfg = ad_config()
    if not cfg['configured']:
        raise DirectoryError('Fill in the domain controllers, search base, account and password first.')
    pins = {h.lower(): v for h, v in ((settings.trusted_certs if settings else None) or {}).items()}
    problems = []
    for host in hosts or cfg['servers']:
        pin = pins.get(host.lower())
        tls = ldap3.Tls(validate=ssl.CERT_NONE if pin else ssl.CERT_REQUIRED, valid_names=[host],
                        ca_certs_file=None if pin else cfg['ca_file'], ca_certs_data=None if pin else cfg['ca_pem'])
        server = ldap3.Server(host, port=LDAPS_PORT, use_ssl=True, tls=tls, get_info=ldap3.NONE, connect_timeout=8)
        conn = ldap3.Connection(server, user=cfg['bind_user'], password=cfg['password'], read_only=True,
                                receive_timeout=60, raise_exceptions=False, auto_referrals=False)
        try:
            conn.open()
            if pin:
                # Pinned certificate: check it BEFORE the password goes over the wire.
                der = conn.socket.getpeercert(binary_form=True)
                if not der or hashlib.sha256(der).hexdigest() != pin:
                    conn.unbind()
                    problems.append(f'{host} is presenting a different certificate than the one you trusted. '
                                    'If the DC\'s certificate was renewed, check and trust the new one.')
                    continue
            if not conn.bind():
                problems.append(f'{host}: {_bind_error(conn.result, cfg["bind_user"])}.')
                conn.unbind()
                continue
            conn.raise_exceptions = True
            return conn, host
        except LDAPException as e:
            cause = e.__context__ if isinstance(e.__context__, (OSError, ssl.SSLError)) else e
            problems.append(_explain(host, cause))
    if not problems:
        problems.append('No domain controllers are set.')
    raise DirectoryError(' '.join(problems))


def _paged(conn, base_dn, search_filter, attributes):
    import ldap3
    from ldap3.core.exceptions import LDAPException
    try:
        for entry in conn.extend.standard.paged_search(base_dn, search_filter, search_scope=ldap3.SUBTREE,
                                                       attributes=attributes, paged_size=500, generator=True):
            if entry.get('type') == 'searchResEntry':
                yield entry['dn'], entry.get('raw_attributes') or {}
    except LDAPException as e:
        raise DirectoryError(f'Reading AD failed: {e}')


def fetch_directory(conn):
    """Every user and computer under the search base, as plain dicts."""
    base = ad_config()['base_dn']
    users = [parse_user(dn, raw) for dn, raw in _paged(conn, base, '(&(objectCategory=person)(objectClass=user))', USER_ATTRS)]
    computers = [parse_computer(dn, raw) for dn, raw in _paged(conn, base, '(objectCategory=computer)', COMPUTER_ATTRS)]
    return dict(users=users, computers=computers)


def read_directory(settings):
    conn, _host = connect(settings)
    try:
        return fetch_directory(conn)
    finally:
        conn.unbind()


# ─── the OU picker ────────────────────────────────────────────────────────────

_SKIP_WORDS = ('disabled', 'test', 'service', 'domain controllers', 'server', 'builtin')


def build_tree(snapshot):
    """Containers that hold users or computers, plus their parent OUs, in
    tree order: [{dn, label, path, depth, users, users_off, computers, stale}].
    dn is lowercase (what the selection stores); label and path keep AD's case."""
    base = (ad_config()['base_dn'] or '').lower()
    nodes = {}

    def node(dn):
        key = dn.lower()
        if key not in nodes:
            root = key == base
            nodes[key] = dict(dn=key, label='(domain root)' if root else dn_rdns(dn)[0].split('=', 1)[-1],
                              path='' if root else short_dn(dn, base), users=0, users_off=0, computers=0, stale=0)
            rdns = dn_rdns(dn)
            for i in range(1, len(rdns)):  # add parent OUs so the list reads as a tree
                parent = ','.join(rdns[i:])
                if parent.lower() == base or not parent.lower().endswith(base):
                    break
                node(parent)
        return nodes[key]

    cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS)
    for u in snapshot['users']:
        n = node(dn_parent(u['dn']))
        n['users' if u['enabled'] else 'users_off'] += 1
    for c in snapshot['computers']:
        n = node(dn_parent(c['dn']))
        n['computers'] += 1
        if not c['enabled'] or not c['last_logon'] or c['last_logon'] < cutoff:
            n['stale'] += 1
    ordered = sorted(nodes.values(), key=lambda n: [r.lower() for r in reversed(dn_rdns(n['path']))])
    for n in ordered:
        n['depth'] = max(0, len(dn_rdns(n['path'])) - 1)
    return ordered


def default_selection(tree):
    """A starting point the admin reviews before the first sync: people from
    OUs that mostly hold people, computers from everywhere, both skipping
    anything that looks disabled, test, service or server."""
    def skip(n):
        return any(word in n['path'].lower() for word in _SKIP_WORDS)
    users = [n['dn'] for n in tree if n['users'] > n['computers'] and not skip(n) and not n['dn'].startswith('cn=users,')]
    computers = [n['dn'] for n in tree if n['computers'] and not skip(n)]
    return users, computers


# ─── sync ─────────────────────────────────────────────────────────────────────

def _user_raw(u):
    return {k: v for k, v in dict(
        first_name=u['first'], last_name=u['last'], role=u['role'], upn=u['upn'], title=u['title'],
        department=u['department'], office=u['office'], grad_year=u['grad_year'],
        last_logon=u['last_logon'].strftime('%Y-%m-%d') if u['last_logon'] else None,
    ).items() if v}


def _other_emails(u):
    primary = u['mail'] or u['upn']
    return [e for e in [u['upn'], *u['aliases']] if e and e != primary]


def run_ad_sync(snapshot=None):
    """Read AD (or use `snapshot`, for tests) and place its users and
    computers. Never creates people or devices: an account with no certain
    match waits on Accounts to Review, a computer with no match waits on the
    Computers page. Returns the summary, also saved on DirectorySettings.
    Commits."""
    settings = ad_settings()
    if snapshot is None:
        snapshot = read_directory(settings)
    now = datetime.utcnow()
    settings.tree = build_tree(snapshot)
    settings.tree_updated_at = now
    if not snapshot['users']:
        db.session.commit()
        raise DirectoryError(f'AD returned no users under {ad_config()["base_dn"]}, so nothing was changed. '
                             'Check the search base.')
    if settings.user_containers is None and settings.computer_containers is None:
        db.session.commit()
        raise DirectoryError('Choose which OUs to sync first, then save.')
    user_scope = set(settings.user_containers or [])
    computer_scope = set(settings.computer_containers or [])
    s = Counter()

    known = {i.external_key: i for i in PersonIdentity.query.filter_by(source=SOURCE)}
    seen = set()
    for u in snapshot['users']:
        if not u['guid']:
            continue
        seen.add(u['guid'])
        existing = known.get(u['guid'])
        fields = dict(username=u['sam'], display_name=u['display'], directory_path=u['dn'],
                      enabled=u['enabled'], raw=_user_raw(u))
        if u['parent'] not in user_scope:
            if existing is not None:
                # Moved out of the chosen OUs (usually into a Disabled OU): keep the
                # link and record what AD says now, but don't place it again.
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.last_synced_at = now
                s['people_out_of_scope'] += 1
            continue
        ident, person, how = place_account(SOURCE, u['guid'], email=u['mail'] or u['upn'],
                                           student_id=u['employee_id'], first_name=u['first'],
                                           last_name=u['last'], role=u['role'], **fields)
        if how in ('review', 'unmatched'):
            # mail didn't match anyone; the UPN or a proxy address might.
            person = next((p for p in (find_person(email=e) for e in _other_emails(u)) if p), None)
            if person:
                ident.person_id, ident.suggested_person_id, how = person.id, None, 'matched'
        if ident.person_id is None:
            if not u['enabled'] and ident.review_status is None:
                ident.review_status = 'inactive'  # disabled and nobody's: not worth anyone's review
            elif u['enabled'] and ident.review_status == 'inactive':
                ident.review_status = None
            if ident.review_status == 'inactive':
                how = 'inactive'
        elif ident.review_status == 'inactive':
            ident.review_status = None
        if person is not None and u['grad_year'] and person.role == 'student' and not person.grad_year:
            person.grad_year = u['grad_year']
            s['grad_years_filled'] += 1
        s[f'people_{how}'] += 1
    for key, ident in known.items():
        if key not in seen and not (ident.raw or {}).get('removed_from_ad'):
            ident.enabled = False
            ident.raw = dict(ident.raw or {}, removed_from_ad=now.strftime('%Y-%m-%d'))
            s['people_removed'] += 1

    known_devices = {r.external_key: r for r in DeviceRecord.query.filter_by(source=SOURCE)}
    seen = set()
    for c in snapshot['computers']:
        if not c['guid']:
            continue
        seen.add(c['guid'])
        existing = known_devices.get(c['guid'])
        fields = dict(join_type='ad', os_name=c['os'], os_version=c['os_version'], directory_path=c['dn'],
                      enabled=c['enabled'], last_seen_at=c['last_logon'],
                      raw={k: v for k, v in dict(dns_name=c['dns_name'], description=c['description']).items() if v})
        if c['parent'] not in computer_scope:
            if existing is not None:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.last_synced_at = now
            continue
        rec, row = place_device_record(SOURCE, c['guid'], serial_number=c['serial'], hostname=c['name'], **fields)
        if row is None and rec.registry_id is None and rec.review_status != 'ignored':
            row = find_registry_row(serial_number=c['name'])  # many districts name PCs by serial / service tag
            if row:
                rec.registry_id = row.id
        s['computers_linked' if rec.registry_id else 'computers_unmatched'] += 1
    for key, rec in known_devices.items():
        if key not in seen and not (rec.raw or {}).get('removed_from_ad'):
            rec.enabled = False
            rec.raw = dict(rec.raw or {}, removed_from_ad=now.strftime('%Y-%m-%d'))
            s['computers_removed'] += 1

    summary = dict(s, users_read=len(snapshot['users']), computers_read=len(snapshot['computers']))
    settings.last_sync_at = now
    settings.last_sync_summary = summary
    settings.last_error = None
    _log_activity('directory_sync', 'Active Directory sync: ' + describe_summary(summary))
    db.session.commit()
    return summary


def describe_summary(s):
    people = (s.get('people_linked', 0) + s.get('people_matched', 0))
    parts = [f'{people} people linked ({s.get("people_matched", 0)} new)',
             f'{s.get("people_review", 0) + s.get("people_unmatched", 0)} accounts to review',
             f'{s.get("computers_linked", 0)} computers linked',
             f'{s.get("computers_unmatched", 0)} computers not in FoxDesk']
    if s.get('grad_years_filled'):
        parts.append(f'{s["grad_years_filled"]} graduation years filled in')
    if s.get('people_removed') or s.get('computers_removed'):
        parts.append(f'{s.get("people_removed", 0)} accounts and {s.get("computers_removed", 0)} computers gone from AD')
    return ', '.join(parts) + '.'
