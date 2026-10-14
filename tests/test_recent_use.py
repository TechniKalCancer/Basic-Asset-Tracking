"""Search goes to the person page, and shows the devices a person last signed in to, newest first."""
from datetime import timedelta

from conftest import A
from foxdesk.clock import utcnow
from foxdesk.services.recent_use import recent_devices


def chromebook(make, tag, users, hours_ago, holder=None):
    row = make.device(tag=tag, holder=holder)
    asset = A.Asset.query.filter_by(asset_tag=tag).one()
    asset.google_recent_users, asset.google_recent_user = users, users[0]
    asset.google_last_activity = utcnow() - timedelta(hours=hours_ago)
    A.db.session.commit()
    return row


def test_recent_devices_newest_first(make):
    ana = make.person('Ana', 'Ruiz', email='ana@students.org')
    A.db.session.add(A.PersonIdentity(person_id=ana.id, source='google', external_key='g-2', email='ana.old@students.org'))
    A.db.session.commit()
    chromebook(make, 'OLD-LATEST', ['ana@students.org'], hours_ago=48)
    chromebook(make, 'NEW-LATEST', ['ana.old@students.org', 'bo@students.org'], hours_ago=2, holder=ana)
    chromebook(make, 'EARLIER', ['bo@students.org', 'ana@students.org'], hours_ago=1)
    chromebook(make, 'NOT-HERS', ['bo@students.org'], hours_ago=1)
    devices = recent_devices([ana])[ana.id]
    assert [d['tag'] for d in devices] == ['NEW-LATEST', 'OLD-LATEST', 'EARLIER'], 'timed ones newest first, then earlier'
    assert devices[0]['latest'] and devices[0]['theirs'] and devices[0]['when'] is not None
    assert devices[2]['when'] is None and devices[2]['signins_since'] == 1


def test_search_opens_the_person_page_and_lists_last_used(client, make):
    ana = make.person('Ana', 'Ruiz', email='ana@students.org')
    make.person('Ana', 'Lopez', email='alopez@students.org')
    chromebook(make, 'CB-77', ['ana@students.org'], hours_ago=3)
    r = client.get('/admin/search?q=Ruiz')
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/admin/people/{ana.id}'), 'one person: straight to their page'
    page = client.get('/admin/search?q=Ana').get_data(as_text=True)
    assert 'Last used (Google sign-ins)' in page and 'CB-77' in page and '3h ago' in page
    assert f'href="/admin/people/{ana.id}"' in page
    people = client.get('/admin/people?q=Ana').get_data(as_text=True)
    assert '<th>Last used</th>' in people and 'CB-77' in people
    assert '<th>Last used</th>' not in client.get('/admin/people').get_data(as_text=True), 'only while searching'
    person = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Recently signed in to' in person and 'Not theirs' in person
