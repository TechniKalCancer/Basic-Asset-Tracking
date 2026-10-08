"""Google sign-in vs assignment check ("Possible Violators")."""
from datetime import datetime, timedelta

import pytest

from conftest import A

NOW = datetime.utcnow()


@pytest.fixture
def scenario(make):
    """One device per rule, each with an expected outcome."""
    P = {k: make.person(first=k, last='Test', email=f'{k.lower()}@test.edu')
         for k in ['Ava', 'Ben', 'Cal', 'Ann', 'Gus', 'Hal', 'Gia', 'Amy', 'Ana', 'Ali', 'Jo', 'Kim']}
    P['Dee'] = make.person(first='Dee', last='Test', email='dee@test.edu', role='staff')
    P['Eve'] = make.person(first='Eve', last='Test', email='eve@test.edu', active=False)
    e = lambda k: P[k].email

    def dev(tag, holder, recent, days_active=1, assigned_days_ago=20, **kw):
        return make.device(tag=tag, holder=holder, assigned_days_ago=assigned_days_ago,
                           google_recent_user=recent[0], google_recent_users=recent,
                           google_last_activity=NOW - timedelta(days=days_active), google_last_sync_at=NOW, **kw)

    dev('S1', P['Ava'], [e('Ben')])                      # swapped with S2
    dev('S2', P['Ben'], [e('Ava'), e('Ben')])
    dev('S3', P['Cal'], [e('Cal')])                      # fine
    dev('S4', P['Ann'], [e('Dee')])                      # staff
    dev('S5', None, [e('Ben')])                          # unassigned, never assigned
    dev('S6', P['Gia'], [e('Gia')], status='lost')       # lost, holder signing in
    dev('S7', P['Gus'], [e('Hal')], assigned_days_ago=1) # previous holder within grace
    A.db.session.add(A.AssignmentHistory(asset_tag='S7', person_id=P['Hal'].id, person_name='Hal Test',
                                         assigned_at=NOW - timedelta(days=200), unassigned_at=NOW - timedelta(days=2)))
    dev('S8', P['Gia'], [e('Hal')], assigned_days_ago=10)
    A.db.session.add(A.AssignmentHistory(asset_tag='S8', person_id=P['Hal'].id, person_name='Hal Test',
                                         assigned_at=NOW - timedelta(days=200), unassigned_at=NOW - timedelta(days=11)))
    dev('S9', P['Amy'], ['stranger@gmail.com'])          # unknown account
    dev('S10', P['Ana'], [e('Eve')])                     # inactive account
    dev('S11', P['Ali'], [e('Kim')], days_active=60)     # outside window
    dev('S12', None, [e('Kim')], days_active=5)          # used before it was returned
    A.db.session.add(A.AssignmentHistory(asset_tag='S12', person_id=P['Kim'].id, person_name='Kim Test',
                                         assigned_at=NOW - timedelta(days=100), unassigned_at=NOW - timedelta(days=2)))
    dev('S13', P['Amy'], [e('Ben')])                     # Ben on a 2nd device
    make.person(first='Ava', last='Test', email='ava.newaccount@test.edu')
    dev('S14', P['Ava'], ['ava.newaccount@test.edu'])    # same name, different record
    old_ben = make.person(first='Ben', last='Test', email='ben.oldaccount@test.edu', active=False)
    old_ben.custom_fields = {'merged_into': P['Ben'].id}
    dev('S15', P['Ben'], ['ben.oldaccount@test.edu'])    # merged-away duplicate of the holder
    A.db.session.commit()
    return P


def mismatches(days=30, **kw):
    with A.app.test_request_context():
        from flask import session
        session.update(admin_logged_in=True, is_admin=True, is_super_admin=True)
        return {m['asset_tag']: m for m in A._signin_mismatches(None, days, **kw)}


def test_categories(scenario):
    got = {t: m['category'] for t, m in mismatches().items()}
    assert got == {
        'S1': 'swapped', 'S2': 'swapped', 'S4': 'staff_signin', 'S5': 'unassigned_in_use', 'S6': 'lost_in_use',
        'S8': 'previous_holder', 'S9': 'unknown_account', 'S10': 'inactive_person', 'S13': 'wrong_student',
        'S14': 'same_name',
    }


def test_repeat_offender_counted_and_sorted_first(scenario):
    m = mismatches()
    assert m['S1']['foreign_count'] == 2 and m['S13']['foreign_count'] == 2
    assert list(m)[0] in ('S1', 'S13')
    assert any('own device: S2' in n for n in m['S1']['note'])


def test_window_and_single_device(scenario):
    assert 'S11' in mismatches(days=90)
    one = list(mismatches(days=90, only_tags=['S13']).values())
    assert len(one) == 1 and one[0]['asset_tag'] == 'S13'


def test_mark_ok_and_reopen(client, scenario):
    client.post('/admin/signin_mismatches/review', data={'asset_tag': 'S13', 'signin_email': 'ben@test.edu', 'note': 'sibling'})
    assert 'S13' not in mismatches()
    assert 'sibling' in client.get('/admin/signin_mismatches?reviewed=1').get_data(as_text=True)
    client.post('/admin/signin_mismatches/review', data={'asset_tag': 'S13', 'signin_email': 'ben@test.edu', 'action': 'reopen'})
    assert 'S13' in mismatches()


def test_dashboard_widget(client, scenario):
    body = client.get('/admin').get_data(as_text=True)
    assert 'Possible Violators' in body and 'on 2 devices' in body
