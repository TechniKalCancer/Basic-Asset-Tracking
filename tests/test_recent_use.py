"""Search shows people and their devices together, and the devices a person last signed in to, newest first."""
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
    assert devices[1]['assignable'] and not devices[0]['assignable'] and not devices[2]['assignable'], \
        'only a free device they were last on'


def test_search_shows_people_and_their_devices(client, make):
    ana = make.person('Ana', 'Jowers', email='ana@students.org')
    make.person('Ana', 'Lopez', email='alopez@students.org')
    own = make.device(tag='CB-OWN', holder=ana)
    chromebook(make, 'CB-77', ['ana@students.org'], hours_ago=3)
    make.device(tag='CB-OTHER')

    page = client.get('/admin/search?q=Jowers').get_data(as_text=True)
    assert 'People (1)' in page and f'href="/admin/people/{ana.id}"' in page, 'one person: still shows the results'
    assets = page[page.index('Assets ('):]
    assert 'Assets (2)' in assets and 'CB-OTHER' not in assets
    assert 'Assigned to Ana Jowers' in assets and own.asset_tag in assets
    assert 'Ana Jowers signed in last, 3h ago' in assets and 'CB-77' in assets
    assert 'Last used (Google sign-ins)' in page and '3h ago' in page

    r = client.get('/admin/search?q=CB-OTHER')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/assets/CB-OTHER/assign'), 'one device: straight to it'
    both = client.get('/admin/search?q=CB-O').get_data(as_text=True)
    assert 'Nobody matches' in both and 'CB-OWN' in both and 'CB-OTHER' in both

    people = client.get('/admin/people?q=Ana').get_data(as_text=True)
    assert '<th>Last used</th>' in people and 'CB-77' in people
    assert '<th>Last used</th>' not in client.get('/admin/people').get_data(as_text=True), 'only while searching'


def test_person_page_assigns_the_device_they_use(client, make, monkeypatch):
    from conftest import patch_everywhere
    patch_everywhere(monkeypatch, 'GOOGLE_SYNC_ENABLED', True)
    ana = make.person('Ana', 'Ruiz', email='ana@students.org')
    bo = make.person('Bo', 'Lee', email='bo@students.org')
    chromebook(make, 'CB-77', ['ana@students.org'], hours_ago=3)
    chromebook(make, 'CB-BO', ['ana@students.org'], hours_ago=30, holder=bo)
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Recently signed in to' in page and 'Google shows them using' in page
    assert 'To someone else' in page and f'href="/admin/people/{bo.id}">Bo Lee' in page
    assert page.count('Assign to Ana') == 1, 'only the free one'

    r = client.post('/admin/assign_from_google', data={'action': 'assign', 'next': 'person',
                                                        'pick': f'CB-77|{ana.id}|ana@students.org'})
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/admin/people/{ana.id}#recent')
    assert A.Asset.query.filter_by(asset_tag='CB-77').one().assigned_to_id == ana.id
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Assign to Ana' not in page and 'To them' in page and 'Google shows them using' not in page
