"""One person, one profile: account matching, the Google people sync, the
Accounts card, Accounts to Review, and the backfill migration."""
import os

import pytest
from flask_migrate import downgrade, upgrade

from conftest import A, ROOT, patch_everywhere


def place(**kw):
    kw.setdefault('source', 'google')
    return A.place_account(**kw)


# ─── matching rules ───────────────────────────────────────────────────────────

def test_matches_by_primary_email_then_alias_then_id(make):
    kid = make.person(first='Ava', last='Smith', email='ava@school.org', external_id='S100')
    _, p, how = place(external_key='g1', email='AVA@school.org')
    assert (p, how) == (kid, 'matched')
    A.add_alias(kid, 'ava.old@school.org')
    _, p, how = place(source='ad', external_key='ad-guid-1', email='ava.old@school.org')
    assert (p, how) == (kid, 'matched')
    _, p, how = place(source='powerschool', external_key='ps-9', student_id='S100')
    assert (p, how) == (kid, 'matched')
    A.db.session.commit()
    _, p, how = place(external_key='g1', email='ava@school.org')
    assert (p, how) == (kid, 'linked')


def test_name_only_match_is_held_not_linked(make):
    kid = make.person(first='Donovan', last='Chavous', email='dchill@school.org')
    ident, p, how = place(external_key='g2', email='dnchavous@school.org', first_name='Donovan', last_name='Chavous')
    assert p is None and how == 'review'
    assert ident.person_id is None and ident.suggested_person_id == kid.id


def test_no_candidate_is_unmatched():
    _, p, how = place(external_key='g3', email='nobody@school.org', first_name='No', last_name='Body')
    assert p is None and how == 'unmatched'


def test_merged_duplicate_resolves_to_survivor(make):
    keep = make.person(first='Journi', last='Parks', email='japarks1@school.org')
    old = make.person(first='Journi', last='Parks', email='japarks@school.org', active=False)
    old.custom_fields = {'merged_into': keep.id}
    A.db.session.commit()
    _, p, _ = place(external_key='g4', email='japarks@school.org')
    assert p == keep


def test_legacy_email_keyed_account_is_rekeyed_not_duplicated(make):
    kid = make.person(email='ava@school.org')
    A.db.session.add(A.PersonIdentity(person_id=kid.id, source='google', external_key='ava@school.org', email='ava@school.org'))
    A.db.session.commit()
    place(external_key='1234567890', email='ava@school.org')
    A.db.session.commit()
    rows = A.PersonIdentity.query.filter_by(source='google').all()
    assert len(rows) == 1 and rows[0].external_key == '1234567890' and rows[0].person_id == kid.id


def test_ignored_account_stays_detached(make):
    make.person(email='ava@school.org')
    ident, _, _ = place(external_key='g5', email='ava@school.org')
    A.db.session.commit()
    A.unlink_account(ident)
    A.db.session.commit()
    _, p, how = place(external_key='g5', email='ava@school.org')
    assert p is None and how == 'ignored'


def test_alias_cannot_belong_to_two_people(make):
    make.person(email='a@school.org')
    b = make.person(email='b@school.org')
    with pytest.raises(ValueError):
        A.add_alias(b, 'a@school.org')


# ─── Google people sync against a fake directory ─────────────────────────────

class FakeDirectory:
    def __init__(self, users):
        self._users = users

    def users(self):
        return self

    def list(self, **kw):
        return self

    def execute(self):
        return {'users': self._users}


def google_user(uid, email, first, last, ou='/Students/Class of 2031', **extra):
    return dict(id=uid, primaryEmail=email, name={'givenName': first, 'familyName': last,
                                                  'fullName': f'{first} {last}'}, orgUnitPath=ou, **extra)


def run_sync(monkeypatch, users):
    patch_everywhere(monkeypatch, '_google_directory_service', lambda scopes: FakeDirectory(users))
    patch_everywhere(monkeypatch, '_classify_org_unit', lambda ou: 'student' if ou.startswith('/Students') else 'staff')
    return A._run_google_people_sync()


def test_google_sync_never_creates_a_same_name_duplicate(monkeypatch, make):
    make.person(first='Trace', last='Thomas', email='tathomas@school.org')
    matched, updated, unmatched, created, held = run_sync(monkeypatch, [
        google_user('u1', 'tathomas2@school.org', 'Trace', 'Thomas'),   # re-issued account, same kid
        google_user('u2', 'brand.new@school.org', 'Brand', 'New'),       # genuinely new student
    ])
    assert (created, held) == (1, 1)
    assert A.Person.query.filter_by(first_name='Trace').count() == 1
    held_acct = A.PersonIdentity.query.filter_by(external_key='u1').one()
    assert held_acct.person_id is None and held_acct.suggested_person.email == 'tathomas@school.org'
    new_acct = A.PersonIdentity.query.filter_by(external_key='u2').one()
    assert new_acct.person.email == 'brand.new@school.org'


def test_google_sync_matches_by_student_id(monkeypatch, make):
    kid = make.person(first='Kris', last='Ready', email='maready@school.org', external_id='77001')
    run_sync(monkeypatch, [google_user('u3', 'kmready@school.org', 'Kris', 'Ready',
                                       externalIds=[{'type': 'organization', 'value': '77001'}])])
    assert A.PersonIdentity.query.filter_by(external_key='u3').one().person_id == kid.id


# ─── screens ─────────────────────────────────────────────────────────────────

def test_accounts_card_add_and_remove(client, make):
    kid = make.person(email='ava@school.org')
    client.post(f'/admin/people/{kid.id}/accounts', data={'action': 'add', 'email': 'Ava.Old@school.org'})
    ident = A.PersonIdentity.query.one()
    assert ident.email == 'ava.old@school.org'
    page = client.get(f'/admin/people/{kid.id}/edit').get_data(as_text=True)
    assert 'ava.old@school.org' in page
    assert client.get('/admin/people?q=ava.old').get_data(as_text=True).count(kid.last_name) >= 1
    client.post(f'/admin/people/{kid.id}/accounts', data={'action': 'remove', 'identity_id': ident.id})
    assert A.PersonIdentity.query.count() == 0


def test_review_link_create_ignore(client, make):
    site = make.site()
    kid = make.person(first='Mark', last='Murray', email='mmmurray1@school.org', site=site)
    held, _, _ = place(external_key='r1', email='mnmurray@school.org', first_name='Mark', last_name='Murray',
                       display_name='Mark Murray')
    new, _, _ = place(external_key='r2', email='new.kid@school.org', first_name='New', last_name='Kid',
                      display_name='New Kid', raw={'first_name': 'New', 'last_name': 'Kid', 'role': 'student'})
    junk, _, _ = place(external_key='r3', email='spare@school.org')
    A.db.session.commit()
    page = client.get('/admin/accounts/review').get_data(as_text=True)
    assert 'mnmurray@school.org' in page and 'Link to Mark' in page

    client.post(f'/admin/accounts/{held.id}/review', data={'action': 'link', 'person_id': kid.id})
    client.post(f'/admin/accounts/{new.id}/review', data={'action': 'create', 'role': 'student', 'site_id': site.id})
    client.post(f'/admin/accounts/{junk.id}/review', data={'action': 'ignore'})
    assert A.PersonIdentity.query.get(held.id).person_id == kid.id
    assert A.Person.query.filter_by(email='new.kid@school.org').one().site_id == site.id
    assert A.PersonIdentity.query.get(junk.id).review_status == 'ignored'
    assert A.accounts_to_review_query().count() == 0


def test_signin_check_counts_alias_as_the_holder(client, make, monkeypatch):
    from datetime import datetime
    patch_everywhere(monkeypatch, 'GOOGLE_SYNC_ENABLED', True)
    kid = make.person(email='ava@school.org')
    A.add_alias(kid, 'ava.second@school.org')
    make.device(tag='AL1', holder=kid, google_recent_user='ava.second@school.org',
                google_recent_users=['ava.second@school.org'], google_last_activity=datetime.utcnow(),
                google_last_sync_at=datetime.utcnow())
    with A.app.test_request_context():
        assert all(m['asset_tag'] != 'AL1' for m in A._signin_mismatches(None, 30))


# ─── backfill migration ──────────────────────────────────────────────────────

def test_backfill_turns_merges_into_aliases_and_seeds_google_accounts(fresh_db):
    migrations = os.path.join(ROOT, 'migrations')
    downgrade(directory=migrations, revision='a324cf393d24')
    conn = A.db.engine.connect()
    conn.execute(A.db.text("INSERT INTO person (id, first_name, last_name, email, role, is_active, insurance_opted_in, created_at, google_last_sync_at, google_org_unit) "
                           "VALUES (1, 'Journi', 'Parks', 'japarks1@school.org', 'student', 1, 0, '2026-01-01', '2026-10-01 08:00:00', '/Students/Class of 2032')"))
    conn.execute(A.db.text("INSERT INTO person (id, first_name, last_name, email, role, is_active, insurance_opted_in, created_at, custom_fields) "
                           "VALUES (2, 'Journi', 'Parks', 'japarks@school.org', 'student', 0, 0, '2026-01-01', '{\"merged_into\": 1}')"))
    conn.commit()
    conn.close()
    upgrade(directory=migrations)
    rows = {(i.source, i.email): i.person_id for i in A.PersonIdentity.query.all()}
    assert rows == {('google', 'japarks1@school.org'): 1, ('alias', 'japarks@school.org'): 1}
