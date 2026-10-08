"""Active Directory: parsing, the OU picker, placing accounts and computers without duplicates, and the pages."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g

from conftest import A, patch_everywhere

BASE = 'DC=fchs,DC=net'


@pytest.fixture
def ad_env(monkeypatch):
    patch_everywhere(monkeypatch, 'AD_BASE_DN', BASE)
    patch_everywhere(monkeypatch, 'AD_SERVERS', ['ad1.fchs.net', 'ad2.fchs.net'])
    patch_everywhere(monkeypatch, 'AD_BIND_USER', 'svc-foxdesk@fchs.net')
    patch_everywhere(monkeypatch, 'AD_BIND_PASSWORD', 'not-a-real-password')
    patch_everywhere(monkeypatch, 'AD_SYNC_ENABLED', True)


def filetime(days_ago):
    when = datetime.utcnow() - timedelta(days=days_ago)
    return str(int((when - datetime(1601, 1, 1)).total_seconds() * 10 ** 7)).encode()


def ad_user(n, ou, first='Pat', last='Doe', mail=None, upn=None, disabled=False, proxies=(), logon_days=3):
    raw = {'objectGUID': [uuid.UUID(int=n).bytes_le], 'sAMAccountName': [f'user{n}'.encode()],
           'givenName': [first.encode()], 'sn': [last.encode()], 'displayName': [f'{first} {last}'.encode()],
           'userAccountControl': [b'514' if disabled else b'512'], 'lastLogonTimestamp': [filetime(logon_days)],
           'proxyAddresses': [p.encode() for p in proxies]}
    if mail:
        raw['mail'] = [mail.encode()]
    if upn:
        raw['userPrincipalName'] = [upn.encode()]
    return A.parse_user(f'CN={first} {last},{ou},{BASE}', raw)


def ad_computer(n, ou, name, serial=None, disabled=False, logon_days=3, os='Windows 11 Enterprise'):
    raw = {'objectGUID': [uuid.UUID(int=1000 + n).bytes_le], 'name': [name.encode()],
           'operatingSystem': [os.encode()], 'userAccountControl': [b'4098' if disabled else b'4096'],
           'lastLogonTimestamp': [filetime(logon_days)] if logon_days is not None else []}
    if serial:
        raw['serialNumber'] = [serial.encode()]
    return A.parse_computer(f'CN={name},{ou},{BASE}', raw)


def choose(users=(), computers=()):
    s = A.ad_settings()
    s.user_containers = [f'{c},{BASE}'.lower() for c in users]
    s.computer_containers = [f'{c},{BASE}'.lower() for c in computers]
    A.db.session.commit()


# ─── parsing ──────────────────────────────────────────────────────────────────

def test_parse_user(ad_env):
    u = ad_user(7, 'OU=2029', first='Ana', last='Ruiz', mail='Ana.Ruiz@FoxCreekStudents.org',
                proxies=['SMTP:ana.ruiz@foxcreekstudents.org', 'smtp:aruiz@fchs.net', 'X500:/o=junk'])
    assert u['guid'] == str(uuid.UUID(int=7)) and u['mail'] == 'ana.ruiz@foxcreekstudents.org'
    assert u['parent'] == 'ou=2029,dc=fchs,dc=net' and u['role'] == 'student' and u['grad_year'] == 2029
    assert u['aliases'] == ['ana.ruiz@foxcreekstudents.org', 'aruiz@fchs.net'] and u['enabled']
    assert (datetime.utcnow() - u['last_logon']).days == 3
    staff = ad_user(8, 'OU=U_Teachers', disabled=True)
    assert staff['role'] == 'staff' and staff['grad_year'] is None and not staff['enabled'] and staff['mail'] is None


def test_errors_are_explained():
    assert 'password is wrong' in A._bind_error({'message': '80090308: LdapErr: ... data 52e, v4563'})
    assert 'no LDAPS certificate' in A._explain('ad1', ConnectionResetError(104, 'Connection reset by peer'))
    assert 'isn\'t trusted' in A._explain('ad1', Exception('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'))


def test_tree_and_suggested_selection(ad_env):
    snap = dict(users=[ad_user(1, 'OU=2029'), ad_user(2, 'OU=U_Teachers'), ad_user(3, 'OU=Disabled,OU=U_Teachers', disabled=True),
                       ad_user(4, 'OU=2030,OU=Students,OU=Disabled', disabled=True), ad_user(5, 'CN=Users'),
                       ad_user(6, 'OU=Service Accounts')],
                computers=[ad_computer(1, 'OU=Lab 170,OU=C_Student_Computers', 'LAB170-01'),
                           ad_computer(2, 'OU=Domain Controllers', 'AD1')])
    tree = A.build_tree(snap)
    by_dn = {n['dn']: n for n in tree}
    assert by_dn['ou=students,ou=disabled,dc=fchs,dc=net']['label'] == 'Students', 'parent OUs keep AD\'s case'
    assert by_dn['ou=2030,ou=students,ou=disabled,dc=fchs,dc=net']['depth'] == 2
    order = [n['dn'] for n in tree]
    assert order.index('ou=u_teachers,dc=fchs,dc=net') < order.index('ou=disabled,ou=u_teachers,dc=fchs,dc=net')
    users, computers = A.default_selection(tree)
    assert set(users) == {'ou=2029,dc=fchs,dc=net', 'ou=u_teachers,dc=fchs,dc=net'}
    assert computers == ['ou=lab 170,ou=c_student_computers,dc=fchs,dc=net']


# ─── people ───────────────────────────────────────────────────────────────────

def test_sync_attaches_accounts_and_never_creates_people(ad_env, make):
    kid = make.person('Ana', 'Ruiz', email='ana.ruiz@foxcreekstudents.org')
    teacher = make.person('Lee', 'Park', role='staff', email='lpark@fcmiddle.net')
    A.add_alias(teacher, 'lpark@fchs.net')
    make.person('Sam', 'Lee', email='sam.lee@foxcreekstudents.org')
    A.db.session.commit()
    people_before = A.Person.query.count()
    choose(users=['OU=2029', 'OU=U_Teachers'])
    snap = dict(users=[
        ad_user(1, 'OU=2029', 'Ana', 'Ruiz', mail='ana.ruiz@foxcreekstudents.org'),
        ad_user(2, 'OU=U_Teachers', 'Lee', 'Park', upn='lpark@fchs.net'),                  # no mail; UPN is an alias
        ad_user(3, 'OU=2029', 'Sam', 'Lee', mail='slee2@foxcreekstudents.org'),            # same name, other email
        ad_user(4, 'OU=2029', 'Gus', 'Ghost', mail='gus@foxcreekstudents.org'),            # nobody
        ad_user(5, 'OU=2029', 'Old', 'Gone', mail='old@foxcreekstudents.org', disabled=True),
        ad_user(6, 'OU=Disabled', 'Out', 'Ofscope', mail='out@foxcreekstudents.org'),      # not a chosen OU
    ], computers=[])
    s = A.run_ad_sync(snapshot=snap)

    assert A.Person.query.count() == people_before, 'AD never creates people'
    ident = lambda n: A.PersonIdentity.query.filter_by(source='ad', external_key=str(uuid.UUID(int=n))).first()  # noqa: E731
    assert ident(1).person_id == kid.id and ident(1).enabled and ident(1).directory_path.startswith('CN=Ana Ruiz,OU=2029')
    assert A.Person.query.get(kid.id).grad_year == 2029, 'blank graduation year filled from the OU'
    assert ident(2).person_id == teacher.id, 'matched through the UPN = an existing alias email'
    assert ident(3).person_id is None and ident(3).suggested_person.first_name == 'Sam', 'name match is only suggested'
    assert ident(4).person_id is None and ident(4).review_status is None, 'unknown and enabled: waits for review'
    assert ident(5).review_status == 'inactive', 'unknown and disabled: kept off the review list'
    assert ident(6) is None, 'out-of-scope accounts are not stored'
    assert {i.external_key for i in A.accounts_to_review_query()} == {str(uuid.UUID(int=3)), str(uuid.UUID(int=4))}
    assert (s['people_matched'], s['people_review'], s['people_unmatched'], s['people_inactive']) == (2, 1, 1, 1)

    again = A.run_ad_sync(snapshot=snap)
    assert again['people_linked'] == 2 and A.PersonIdentity.query.filter_by(source='ad').count() == 5, 'idempotent'


def test_disabled_and_removed_accounts_are_followed(ad_env, make, client):
    kid = make.person('Ana', 'Ruiz', email='ana.ruiz@foxcreekstudents.org')
    make.device(holder=kid)
    staff = make.person('Lee', 'Park', role='staff', email='lpark@fchs.net')
    choose(users=['OU=2029', 'OU=U_Teachers'])
    A.run_ad_sync(snapshot=dict(users=[ad_user(1, 'OU=2029', 'Ana', 'Ruiz', mail='ana.ruiz@foxcreekstudents.org'),
                                       ad_user(2, 'OU=U_Teachers', 'Lee', 'Park', mail='lpark@fchs.net')], computers=[]))
    # Ana's account moves to an unchosen Disabled OU; Lee's is deleted from AD.
    s = A.run_ad_sync(snapshot=dict(users=[ad_user(1, 'OU=2029,OU=Students,OU=Disabled', 'Ana', 'Ruiz',
                                                   mail='ana.ruiz@foxcreekstudents.org', disabled=True)], computers=[]))
    ana = A.PersonIdentity.query.filter_by(person_id=kid.id, source='ad').one()
    lee = A.PersonIdentity.query.filter_by(person_id=staff.id, source='ad').one()
    assert ana.enabled is False and 'OU=Disabled' in ana.directory_path and s['people_out_of_scope'] == 1
    assert lee.enabled is False and lee.raw['removed_from_ad'] and s['people_removed'] == 1

    body = client.get('/admin/directory/people').get_data(as_text=True)
    assert 'Ana Ruiz' in body and 'Lee Park' in body and 'gone from AD' in body
    assert 'badge badge-red' in body, 'Ana still holds a device'


def test_sync_refuses_empty_reads_and_unchosen_scope(ad_env, make):
    with pytest.raises(A.DirectoryError, match='Choose which OUs'):
        A.run_ad_sync(snapshot=dict(users=[ad_user(1, 'OU=2029')], computers=[]))
    assert A.ad_settings().tree, 'the OU list is still saved for the picker'
    choose(users=['OU=2029'])
    A.run_ad_sync(snapshot=dict(users=[ad_user(1, 'OU=2029', mail='x@foxcreekstudents.org')], computers=[]))
    with pytest.raises(A.DirectoryError, match='no users'):
        A.run_ad_sync(snapshot=dict(users=[], computers=[]))
    assert not (A.PersonIdentity.query.filter_by(source='ad').one().raw or {}).get('removed_from_ad'), \
        'an empty read must not mark everyone as removed'


# ─── computers ────────────────────────────────────────────────────────────────

def test_computers_match_by_serial_or_name(ad_env, make, client):
    named = make.device(serial='ABC1234', device_type='laptop')
    attr = make.device(serial='5CG1234XYZ', device_type='laptop')
    loose = make.device(serial='NOTINAD1', device_type='laptop')
    make.device(serial='CHROME1')  # a Chromebook: never expected in AD
    choose(computers=['OU=Staff', 'OU=Lab'])
    snap = dict(users=[ad_user(1, 'OU=2029')], computers=[
        ad_computer(1, 'OU=Staff', 'abc1234'),                                   # named by its serial
        ad_computer(2, 'OU=Staff', 'LIB-PC-02', serial='5CG1234XYZ'),            # serialNumber attribute
        ad_computer(3, 'OU=Lab', 'LAB170-01', logon_days=200),                   # unknown and stale
        ad_computer(4, 'OU=Domain Controllers', 'AD1'),                          # not chosen
    ])
    s = A.run_ad_sync(snapshot=snap)
    rec = lambda n: A.DeviceRecord.query.filter_by(source='ad', external_key=str(uuid.UUID(int=1000 + n))).first()  # noqa: E731
    assert rec(1).registry_id == named.id and rec(1).join_type == 'ad'
    assert rec(2).registry_id == attr.id and rec(2).serial_number == '5CG1234XYZ'
    assert rec(3).registry_id is None and rec(4) is None
    assert (s['computers_linked'], s['computers_unmatched']) == (2, 1)

    page = lambda tab: client.get(f'/admin/directory/computers?show={tab}').get_data(as_text=True)  # noqa: E731
    assert 'abc1234' in page('linked') and 'LAB170-01' in page('unmatched') and 'LAB170-01' in page('stale')
    workgroup = page('workgroup')
    assert loose.asset_tag in workgroup and named.asset_tag not in workgroup and 'CHROME1' not in workgroup
    assert 'AD-joined' in client.get(f'/admin/assets/{named.asset_tag}/assign').get_data(as_text=True)

    client.post(f'/admin/directory/computers/{rec(3).id}', data={'action': 'link', 'asset_tag': loose.asset_tag.lower()})
    assert rec(3).registry_id == loose.id
    client.post(f'/admin/directory/computers/{rec(1).id}', data={'action': 'ignore'})
    A.run_ad_sync(snapshot=snap)
    assert rec(1).registry_id is None and rec(1).review_status == 'ignored', 'an unlinked match stays unlinked'


# ─── pages ────────────────────────────────────────────────────────────────────

def test_setup_page_before_configuration(client):
    body = client.get('/admin/directory').get_data(as_text=True)
    assert 'Not configured' in body and 'AD_BIND_PASSWORD=' in body


def test_pick_containers_and_sync_now(ad_env, client, make, monkeypatch):
    make.person('Ana', 'Ruiz', email='ana.ruiz@foxcreekstudents.org')
    snap = dict(users=[ad_user(1, 'OU=2029', 'Ana', 'Ruiz', mail='ana.ruiz@foxcreekstudents.org'),
                       ad_user(2, 'OU=Disabled', disabled=True)],
                computers=[ad_computer(1, 'OU=Lab', 'LAB-1')])
    patch_everywhere(monkeypatch, 'read_directory', lambda settings: snap)
    client.post('/admin/directory/refresh')
    body = client.get('/admin/directory').get_data(as_text=True)
    assert 'These checkboxes are a suggestion' in body and 'value="ou=2029,dc=fchs,dc=net" checked' in body

    client.post('/admin/directory/scope', data={'users': ['ou=2029,dc=fchs,dc=net', 'ou=made-up,dc=x'],
                                                'computers': ['ou=lab,dc=fchs,dc=net']})
    s = A.ad_settings()
    assert s.user_containers == ['ou=2029,dc=fchs,dc=net'] and s.computer_containers == ['ou=lab,dc=fchs,dc=net']

    r = client.post('/admin/directory/sync', follow_redirects=True)
    assert b'Synced: 1 people linked (1 new)' in r.data
    assert A.ad_settings().last_sync_summary['people_matched'] == 1

    patch_everywhere(monkeypatch, 'read_directory', lambda settings: (_ for _ in ()).throw(A.DirectoryError('ad1 timed out')))
    r = client.post('/admin/directory/sync', follow_redirects=True)
    assert b'ad1 timed out' in r.data and A.ad_settings().last_error == 'ad1 timed out'


def test_certificate_check_and_trust(ad_env, client, monkeypatch):
    cert = dict(sha256='ab' * 32, sha1='CD' * 20, subject='CN=ad2.fchs.net', issuer='CN=ad2.fchs.net',
                not_after='2031-10-08', names=['ad2.fchs.net'], self_signed=True)

    def fake_cert(host, timeout=6):
        if host == 'ad1.fchs.net':
            raise A.DirectoryError('ad1.fchs.net answered on port 636 but has no LDAPS certificate yet.')
        return cert
    patch_everywhere(monkeypatch, 'server_certificate', fake_cert)
    patch_everywhere(monkeypatch, 'verified_by_ca', lambda host, timeout=6: False)
    body = client.post('/admin/directory/check').get_data(as_text=True)
    assert 'has no LDAPS certificate yet' in body and 'CD' * 20 in body and 'Trust this certificate' in body

    r = client.post('/admin/directory/trust', data={'host': 'ad2.fchs.net', 'sha256': 'ff' * 32}, follow_redirects=True)
    assert b'different certificate' in r.data and not A.ad_settings().trusted_certs, 'only the certificate shown can be trusted'
    client.post('/admin/directory/trust', data={'host': 'evil.example.com', 'sha256': cert['sha256']})
    assert not A.ad_settings().trusted_certs
    client.post('/admin/directory/trust', data={'host': 'ad2.fchs.net', 'sha256': cert['sha256']})
    assert A.ad_settings().trusted_certs == {'ad2.fchs.net': cert['sha256']}
    client.post('/admin/directory/untrust', data={'host': 'ad2.fchs.net'})
    assert A.ad_settings().trusted_certs == {}


def test_accounts_review_filters_by_source(ad_env, client, make):
    choose(users=['OU=2029'])
    A.run_ad_sync(snapshot=dict(users=[ad_user(1, 'OU=2029', 'Gus', 'Ghost', mail='gus@foxcreekstudents.org')], computers=[]))
    A.db.session.add(A.PersonIdentity(source='google', external_key='g1', email='other@foxcreekstudents.org',
                                      display_name='Gina Google'))
    A.db.session.commit()
    body = client.get('/admin/accounts/review?source=ad').get_data(as_text=True)
    assert 'gus@foxcreekstudents.org' in body and 'Gina Google' not in body
    assert 'Active Directory' in body and 'Google Workspace' in body, 'both sources offered as filters'


def test_feature_switch_hides_ad(ad_env, client):
    assert client.get('/admin/directory').status_code == 200
    A.set_feature('active_directory', False, 'test')
    A.db.session.commit()
    g.pop('_feature_overrides', None)
    assert client.get('/admin/directory').status_code == 404
    assert client.get('/admin/directory/computers').status_code == 404


def test_scheduled_sync_runs_ad(ad_env, make, monkeypatch):
    make.person('Ana', 'Ruiz', email='ana.ruiz@foxcreekstudents.org')
    choose(users=['OU=2029'])
    patch_everywhere(monkeypatch, 'read_directory', lambda settings: dict(
        users=[ad_user(1, 'OU=2029', 'Ana', 'Ruiz', mail='ana.ruiz@foxcreekstudents.org')], computers=[]))
    schedule = A._get_or_create_sync_schedule('ad')
    schedule.enabled = True
    A.db.session.commit()
    A._run_due_scheduled_syncs()
    assert A.SyncSchedule.query.filter_by(sync_type='ad').one().last_run_summary.startswith('1 people linked')
