"""Assign from Google sign-ins: who each unassigned in-use device should go to, and how sure."""
from datetime import timedelta

from conftest import A, patch_everywhere
from foxdesk.clock import utcnow


def in_use(make, *emails, days_ago=1, **kw):
    """An unassigned device whose Google recent users are `emails` (newest first)."""
    row = make.device(**kw)
    asset = A.Asset.query.filter_by(asset_tag=row.asset_tag).one()
    asset.google_recent_users = list(emails)
    asset.google_recent_user = emails[0]
    asset.google_last_activity = utcnow() - timedelta(days=days_ago)
    A.db.session.commit()
    return row


def proposals():
    return {p['registry_row'].asset_tag: p for p in A._assignment_proposals(None)}


def test_tiers(make):
    solo = make.person('Ana', 'Solo')
    gone = make.person('Old', 'Grad', active=False)
    sharer, rival = make.person('Sam', 'Share'), make.person('Rae', 'Rival')
    holder = make.person('Hal', 'Holder')
    make.device(holder=holder)
    teacher = make.person('Tess', 'Teacher', role='staff')
    two = make.person('Two', 'Devices')

    clear = in_use(make, solo.email, gone.email)          # earlier user graduated: still clear
    shared = in_use(make, sharer.email, rival.email)      # rival has no device of their own
    has_one = in_use(make, holder.email)
    cart = in_use(make, teacher.email)
    a, b = in_use(make, two.email), in_use(make, two.email)
    unknown = in_use(make, 'stranger@elsewhere.org')
    stale = in_use(make, solo.email, days_ago=120)
    lost = in_use(make, solo.email, status='lost')

    mover, newer = make.person('Mo', 'Ver'), make.person('Nia', 'New')
    moved_on = in_use(make, newer.email, mover.email)     # Mo is now the latest user on another device
    in_use(make, mover.email, days_ago=2)

    p = proposals()
    assert p[moved_on.asset_tag]['tier'] == 'high', 'an earlier user who now uses another device is no rival'
    assert p[clear.asset_tag]['tier'] == 'high' and p[clear.asset_tag]['person'].id == solo.id
    assert any('moved on' in e for e, _ in p[clear.asset_tag]['evidence'])
    assert p[shared.asset_tag]['tier'] == 'medium' and 'Rae Rival' in p[shared.asset_tag]['evidence'][0][0]
    assert p[a.asset_tag]['tier'] == p[b.asset_tag]['tier'] == 'medium'
    assert p[has_one.asset_tag]['tier'] == 'low' and p[cart.asset_tag]['tier'] == 'low'
    for left_out in (unknown, stale, lost):
        assert left_out.asset_tag not in p


def test_loaners_and_merged_accounts(make):
    kid = make.person('Kai', 'Kid')
    old = make.person('Kai', 'Kid', email='kai.old@example.edu', active=False, custom_fields={'merged_into': kid.id})
    pool = in_use(make, kid.email)
    A.db.session.get(A.AssetRegistry, pool.id).is_loaner = True
    via_old = in_use(make, old.email)
    A.db.session.commit()
    p = proposals()
    assert pool.asset_tag not in p, 'loaners go through checkouts'
    assert p[via_old.asset_tag]['person'].id == kid.id, 'an old merged account counts as the survivor'


def test_assign_and_skip(client, make, monkeypatch):
    patch_everywhere(monkeypatch, 'GOOGLE_SYNC_ENABLED', True)
    events = []
    patch_everywhere(monkeypatch, 'emit', lambda trigger, subject, **kw: events.append((trigger, subject.asset_tag)))
    ana, ben = make.person('Ana', 'Solo'), make.person('Ben', 'Solo')
    d1, d2 = in_use(make, ana.email), in_use(make, ben.email)
    body = client.get('/admin/assign_from_google').get_data(as_text=True)
    assert 'Ready to assign' in body and d1.asset_tag in body and 'checked' in body
    found = client.get('/admin/assign_from_google?q=ben').get_data(as_text=True)
    assert d2.asset_tag in found and d1.asset_tag not in found
    assert 'Nothing here matches' in client.get('/admin/assign_from_google?q=zzz').get_data(as_text=True)

    r = client.post('/admin/assign_from_google?tier=high', data={'action': 'assign', 'pick': [f'{d1.asset_tag}|{ana.id}|{ana.email}']},
                    follow_redirects=True)
    assert b'Assigned 1 device' in r.data
    asset = A.Asset.query.filter_by(asset_tag=d1.asset_tag).one()
    assert asset.assigned_to_id == ana.id and asset.status == 'assigned'
    assert A.AssignmentHistory.query.filter_by(asset_tag=d1.asset_tag, person_id=ana.id).count() == 1
    assert events == [('device.assigned', d1.asset_tag)]

    client.post('/admin/assign_from_google', data={'action': 'skip', 'pick': [f'{d2.asset_tag}|{ben.id}|{ben.email}']})
    assert A.SigninReview.query.filter_by(asset_tag=d2.asset_tag, signin_email=ben.email).one().note == A.ASSIGN_SKIP_NOTE
    p = proposals()
    assert d1.asset_tag not in p and d2.asset_tag not in p

    r = client.post('/admin/assign_from_google', data={'action': 'assign', 'pick': [f'{d1.asset_tag}|{ben.id}|{ben.email}']},
                    follow_redirects=True)
    assert b'Left alone' in r.data and A.Asset.query.filter_by(asset_tag=d1.asset_tag).one().assigned_to_id == ana.id

