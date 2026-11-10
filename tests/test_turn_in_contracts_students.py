"""End-of-year turn-in, contracts with renewal reminders, the new-student-without-a-device
alert, the password lookup button, and a manual status change closing a repair's ticket."""
from datetime import date, datetime, timedelta

from conftest import A
from foxdesk.clock import utcnow
from foxdesk.services import overdue as od


def make_user(username, email, super_admin=False):
    u = A.User(username=username, email=email, password_hash='x', is_super_admin=super_admin)
    A.db.session.add(u)
    A.db.session.commit()
    return u


def test_turn_in_boxes_outstanding_and_hand_back(client, make):
    site = make.site('Middle')
    kids = [make.person(f'Kid{i}', 'Ruiz', site=site) for i in range(3)]
    devices = [make.device(site=site, holder=k) for k in kids]
    client.post('/admin/turn-in', data={'name': 'End of year 2027', 'site_id': site.id, 'box_size': '2'})
    session = A.CollectionSession.query.one()
    url = f'/admin/turn-in/{session.id}'
    page = client.get(url).get_data(as_text=True)
    assert 'Still out (3)' in page and 'next: box 1, slot 1' in page

    r = client.post(url, data={'scan_value': devices[0].asset_tag, 'mode': 'in'}, follow_redirects=True)
    assert b'box 1, slot 1' in r.data
    client.post(url, data={'scan_value': devices[1].serial_number, 'mode': 'in', 'note': 'no charger'})
    r = client.post(url, data={'scan_value': devices[2].asset_tag, 'mode': 'in'}, follow_redirects=True)
    assert b'box 2, slot 1' in r.data and b'Still out (0)' in r.data
    r = client.post(url, data={'scan_value': devices[0].asset_tag, 'mode': 'in'}, follow_redirects=True)
    assert b'already in box 1, slot 1' in r.data and A.CollectionScan.query.count() == 3
    assert A.Asset.query.filter_by(asset_tag=devices[0].asset_tag).one().assigned_to_id == kids[0].id, 'stays assigned over summer'
    assert b'isn&#39;t a device at Middle' in client.post(url, data={'scan_value': 'NOPE-999', 'mode': 'in'}, follow_redirects=True).data

    boxes = client.get(f'{url}/boxes').get_data(as_text=True)
    assert boxes.count('class="bx-box"') == 2 and 'Kid0 Ruiz' in boxes
    found = client.get(f'{url}?q=Kid2').get_data(as_text=True)
    assert 'box 2, slot 1' in found
    r = client.post(url, data={'scan_value': devices[2].asset_tag, 'mode': 'back'}, follow_redirects=True)
    assert b'back to Kid2 Ruiz' in r.data
    assert A.CollectionScan.query.filter_by(asset_tag=devices[2].asset_tag).one().handed_back_at

    client.post('/admin/turn-in', data={'name': 'Graduating 8th', 'site_id': site.id, 'returns_device': 'on'})
    leaving = A.CollectionSession.query.filter_by(name='Graduating 8th').one()
    client.post(f'/admin/turn-in/{leaving.id}', data={'scan_value': devices[1].asset_tag, 'mode': 'in'})
    asset = A.Asset.query.filter_by(asset_tag=devices[1].asset_tag).one()
    assert asset.assigned_to_id is None and asset.status == 'available', 'leaving students: unassigned at turn-in'
    client.post(f'{url}/close')
    closed = client.post(url, data={'scan_value': devices[1].asset_tag, 'mode': 'in'}, follow_redirects=True)
    assert b'This turn-in is closed' in closed.data


def test_contracts_and_renewal_reminders(client, make, sent_emails):
    boss = make_user('boss', 'boss@district.org', super_admin=True)
    ends = date.today() + timedelta(days=30)
    r = client.post('/admin/contracts/new', data={'name': 'Lightspeed Filter', 'vendor': 'Lightspeed', 'kind': 'software',
                                                  'ends_on': ends.isoformat(), 'cost': '4800', 'billing': 'yearly',
                                                  'notice_days': '60', 'owner': 'gmcghee'}, follow_redirects=True)
    assert b'Saved Lightspeed Filter. Reminders at 60, 30, 14, 7, 1, 0 days' in r.data
    listing = client.get('/admin/contracts').get_data(as_text=True)
    assert 'Lightspeed Filter' in listing and '30 days left' in listing and '$4,800' in listing
    assert b'It ends before it starts' in client.post('/admin/contracts/new', data={
        'name': 'Bad', 'starts_on': '2027-01-01', 'ends_on': '2026-01-01'}, follow_redirects=True).data

    from foxdesk.services.contracts import due_reminders
    today = date.today()
    assert [d['contract'].name for d in due_reminders(today, today - timedelta(days=1))] == ['Lightspeed Filter'], 'crossed 30 today'
    assert due_reminders(today + timedelta(days=1), today) == [], 'nothing crossed tomorrow'
    assert len(due_reminders(today + timedelta(days=23), today + timedelta(days=20))) == 1, 'the 7-day point over a weekend'

    sent, _ = od.send_tech_alerts(today=today, previous=today - timedelta(days=1))
    assert sent == 1 and sent_emails[0][0] == boss.email
    assert '1 renewal' in sent_emails[0][1] and 'Lightspeed Filter (Lightspeed) ends' in sent_emails[0][2]
    c = A.Contract.query.filter_by(name='Lightspeed Filter').one()
    client.post(f'/admin/contracts/{c.id}/edit', data={'action': 'toggle'})
    assert not A.db.session.get(A.Contract, c.id).is_active and due_reminders(today, today - timedelta(days=1)) == []


def test_new_student_without_a_device(client, make, sent_emails):
    site = make.site('Middle')
    site.techs = [make_user('mstech', 'mstech@district.org')]
    settings = od.overdue_settings(create=True)
    settings.new_student_since = utcnow() - timedelta(days=10)
    old = make.person('Old', 'Timer', site=site)
    old.created_at = utcnow() - timedelta(days=30)
    waiting = make.person('New', 'Kid', site=site, grad_year=2031)
    waiting.created_at = utcnow() - timedelta(days=2)
    fresh = make.person('Just', 'Added', site=site)
    has_one = make.person('Has', 'Device', site=site)
    has_one.created_at = utcnow() - timedelta(days=3)
    make.device(site=site, holder=has_one)
    A.db.session.commit()

    found = od.new_students_without_device()
    assert [p['name'] for p in found[site.id]] == ['New Kid'], 'old backlog, brand-new and covered students are left out'
    od.send_tech_alerts(today=date.today())
    assert 'New students without a device (1)' in sent_emails[0][2] and 'New Kid (class of 2031)' in sent_emails[0][2]
    assert 'New Kid' in client.get('/admin/overdue').get_data(as_text=True)

    page = client.get(f'/admin/people/{waiting.id}').get_data(as_text=True)
    assert 'No device yet' in page
    client.post(f'/admin/people/{waiting.id}/device_exempt', data={'reason': 'Brings their own'})
    assert A.db.session.get(A.Person, waiting.id).device_exempt_reason == 'Brings their own'
    assert od.new_students_without_device() == {}
    client.post(f'/admin/people/{waiting.id}/device_exempt', data={'action': 'clear'})
    assert A.db.session.get(A.Person, waiting.id).device_exempt_reason is None
    assert fresh.id not in [p['id'] for p in od.new_students_without_device().get(site.id, [])]


def test_password_lookup_button(client, make):
    site = make.site('Middle')
    ana = make.person('Ana', 'Ruiz', site=site, email='aruiz@students.org')
    assert 'Look up password' not in client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    r = client.post(f'/admin/sites/{site.id}/edit', data={'name': 'Middle', 'password_lookup_url': 'https://evil.example/x'},
                    follow_redirects=True)
    assert b'script.google.com' in r.data
    client.post(f'/admin/sites/{site.id}/edit', data={'name': 'Middle',
                                                      'password_lookup_url': 'https://script.google.com/a/macros/d.org/s/ABC/exec?q=old'})
    assert A.db.session.get(A.Site, site.id).password_lookup_url == 'https://script.google.com/a/macros/d.org/s/ABC/exec'
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'href="https://script.google.com/a/macros/d.org/s/ABC/exec?q=aruiz"' in page and 'Look up password' in page
    staff = make.person('Pat', 'Teach', role='staff', site=site)
    assert 'Look up password' not in client.get(f'/admin/people/{staff.id}').get_data(as_text=True)


def test_manual_status_change_resolves_the_repair_ticket(client, make):
    row = make.device()
    client.post(f'/admin/assets/{row.asset_tag}/repairs/send', data={'issue_description': 'Screen'})
    repair = A.Repair.query.one()
    client.post(f'/admin/assets/{row.asset_tag}/status', data={'status': 'available'})
    assert A.db.session.get(A.Repair, repair.id).returned_at
    assert A.db.session.get(A.Ticket, repair.ticket_id).status == 'resolved'
