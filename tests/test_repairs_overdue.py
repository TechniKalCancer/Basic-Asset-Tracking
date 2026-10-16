"""Repair statuses and notes, the two-week repair limit, the daily overdue email
to each school's techs, and no student emails about repair loaners."""
from datetime import date, datetime, timedelta

from conftest import A
from foxdesk.clock import utcnow
from foxdesk.services import overdue as od
from foxdesk.services.sla import sla_settings


def send_to_repair(client, make, **form):
    row = make.device(**{k: form.pop(k) for k in ('site', 'holder') if k in form})
    data = dict(issue_description="Won't power on")
    data.update(form)
    r = client.post(f'/admin/assets/{row.asset_tag}/repairs/send', data=data, follow_redirects=True)
    return row, A.Repair.query.filter_by(asset_tag=row.asset_tag).first(), r.get_data(as_text=True)


def make_user(username, email=None, super_admin=False):
    u = A.User(username=username, email=email, password_hash='x', is_super_admin=super_admin, can_repairs=True)
    A.db.session.add(u)
    A.db.session.commit()
    return u


def test_two_week_limit(client, make):
    _, repair, _ = send_to_repair(client, make)
    assert repair.expected_return_at == repair.sent_at.date() + timedelta(days=14), 'blank = the limit'
    assert repair.status == 'received'

    too_late = (date.today() + timedelta(days=30)).isoformat()
    _, nothing, page = send_to_repair(client, make, expected_return_at=too_late)
    assert nothing is None and 'at most 14 days' in page

    r = client.post(f'/admin/repairs/{repair.id}/edit', data={'issue_description': 'x', 'expected_return_at': too_late},
                    follow_redirects=True)
    assert b'at most 14 days' in r.data and A.db.session.get(A.Repair, repair.id).expected_return_at != date.fromisoformat(too_late)
    _, vendor, _ = send_to_repair(client, make, ticket_number='RMA-1')
    assert vendor.status == 'at_vendor', 'an RMA number means it went to the vendor'

    client.post('/admin/overdue/settings', data={'repair_max_days': '21', 'alert_hour': '7', 'alerts_enabled': 'on'})
    _, longer, _ = send_to_repair(client, make, expected_return_at=(date.today() + timedelta(days=20)).isoformat())
    assert longer is not None, 'the limit is a setting'


def test_status_and_notes_timeline(client, make):
    _, repair, _ = send_to_repair(client, make)
    url = f'/admin/repairs/{repair.id}/update'
    r = client.post(url, data={'status': 'waiting_parts', 'note': 'Ordered a screen, RMA 4471.'}, follow_redirects=True)
    assert b'Status is now Waiting on parts' in r.data
    note = A.RepairNote.query.one()
    assert (note.status_from, note.status_to, note.body) == ('received', 'waiting_parts', 'Ordered a screen, RMA 4471.')
    comments = A.TicketComment.query.filter_by(ticket_id=repair.ticket_id).all()
    assert any('Received → Waiting on parts' in c.body and 'RMA 4471' in c.body for c in comments), 'mirrored to the ticket'

    assert b'Pick a new status or write a note' in client.post(url, data={'status': 'waiting_parts'}, follow_redirects=True).data
    assert b'Choose a valid status' in client.post(url, data={'status': 'bogus'}, follow_redirects=True).data
    client.post(url, data={'note': 'Part arrived.'})
    page = client.get(f'/admin/repairs/{repair.id}').get_data(as_text=True)
    assert 'Waiting on parts' in page and 'Part arrived.' in page and page.index('Part arrived.') < page.index('Ordered a screen')

    client.post(f'/admin/repairs/{repair.id}/return', data={'outcome': 'fixed', 'notes': 'New screen'})
    assert b'is closed' in client.post(url, data={'status': 'ready'}, follow_redirects=True).data
    client.post(url, data={'note': 'Student picked it up.'})
    assert A.RepairNote.query.filter(A.RepairNote.body.like('Returned: Fixed%')).count() == 1
    assert A.RepairNote.query.filter_by(body='Student picked it up.').count() == 1


def test_repairs_list_status_and_overdue(client, make):
    _, fresh, _ = send_to_repair(client, make)
    _, old, _ = send_to_repair(client, make)
    old.sent_at, old.expected_return_at = utcnow() - timedelta(days=20), None
    A.db.session.commit()
    page = client.get('/admin/repairs').get_data(as_text=True)
    assert 'Due Back' in page and '6d overdue' in page and 'Received' in page
    overdue = client.get('/admin/repairs?status=overdue').get_data(as_text=True)
    assert old.asset_tag in overdue and fresh.asset_tag not in overdue


def test_overdue_by_school_and_tech_email(client, make, sent_emails):
    ms, hs = make.site('Middle'), make.site('High')
    tech = make_user('mstech', 'mstech@district.org')
    hs_tech = make_user('hstech', 'hstech@district.org')
    make_user('boss', 'boss@district.org', super_admin=True)
    no_email = make_user('newtech')
    ms.techs = [tech, no_email]
    hs.techs = [hs_tech]
    kid = make.person('Ana', 'Ruiz', site=ms)
    _, repair, _ = send_to_repair(client, make, site=ms, holder=kid)
    repair.sent_at, repair.expected_return_at = utcnow() - timedelta(days=20), None
    loaner = make.device(site=ms)
    A.db.session.add(A.LoanerCheckout(asset_tag=loaner.asset_tag, person_id=kid.id, person_name=kid.full_name,
                                      checked_out_at=utcnow() - timedelta(days=10), due_date=date.today() - timedelta(days=3),
                                      repair_id=repair.id))
    stray = make.device()
    A.db.session.add(A.LoanerCheckout(asset_tag=stray.asset_tag, person_id=kid.id, person_name=kid.full_name,
                                      checked_out_at=utcnow() - timedelta(days=9), due_date=date.today() - timedelta(days=1)))
    A.db.session.commit()

    by_site = od.overdue_by_site()
    assert list(by_site) == [ms.id, None], 'only schools with something overdue; no-school last'
    section = by_site[ms.id]
    assert section['repairs'][0]['days'] == 6 and section['repairs'][0]['loaner'].startswith(loaner.asset_tag)
    assert section['loaners'][0]['for_repair']

    sent, failed = od.send_tech_alerts()
    assert (sent, failed) == (2, 0)
    to = {t: (s, b) for t, s, b in sent_emails}
    assert set(to) == {'mstech@district.org', 'boss@district.org'}, 'the HS tech has nothing; no email, no send'
    subject, body = to['mstech@district.org']
    assert '2 overdue items' in subject and 'Middle — 2 overdue' in body and repair.asset_tag in body
    assert 'repair loaner, student not emailed' in body and stray.asset_tag not in body
    assert stray.asset_tag in to['boss@district.org'][1], 'devices with no school go to super admins'

    page = client.get('/admin/overdue').get_data(as_text=True)
    assert 'Middle' in page and 'no email' in page and 'Devices with no school set' in page


def test_daily_email_once_on_school_days(make, sent_emails, monkeypatch):
    site = make.site('Middle')
    site.techs = [make_user('t', 't@district.org')]
    old = make.device(site=site)
    A.db.session.add(A.Repair(asset_tag=old.asset_tag, issue_description='x', sent_at=utcnow() - timedelta(days=30)))
    A.db.session.commit()
    wed_8am = datetime(2026, 10, 14, 12, 0)   # 8:00 New York
    wed_6am = datetime(2026, 10, 14, 10, 0)
    sat_9am = datetime(2026, 10, 17, 13, 0)
    assert not od.maybe_send_daily(wed_6am), 'before the hour'
    assert not od.maybe_send_daily(sat_9am), 'not a school day'
    assert od.maybe_send_daily(wed_8am) and len(sent_emails) == 1
    assert not od.maybe_send_daily(wed_8am + timedelta(hours=2)) and len(sent_emails) == 1, 'once a day'
    assert od.maybe_send_daily(wed_8am + timedelta(days=1)) and len(sent_emails) == 2

    sla = sla_settings(create=True)
    sla.closed_dates = '2026-10-16'
    A.db.session.commit()
    assert not od.maybe_send_daily(datetime(2026, 10, 16, 12, 0)), 'closed dates are skipped'
    od.overdue_settings(create=True).alerts_enabled = False
    A.db.session.commit()
    assert not od.maybe_send_daily(datetime(2026, 10, 19, 12, 0)), 'switched off'


def test_repair_loaner_students_are_not_emailed(client, make, sent_emails):
    kid, other = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee')
    _, repair, _ = send_to_repair(client, make, holder=kid)
    for_repair, plain = make.device(), make.device()
    late = date.today() - timedelta(days=2)
    A.db.session.add_all([
        A.LoanerCheckout(asset_tag=for_repair.asset_tag, person_id=kid.id, person_name=kid.full_name, due_date=late,
                         checked_out_at=utcnow() - timedelta(days=5), repair_id=repair.id),
        A.LoanerCheckout(asset_tag=plain.asset_tag, person_id=other.id, person_name=other.full_name, due_date=late,
                         checked_out_at=utcnow() - timedelta(days=5)),
    ])
    A.db.session.commit()
    sent_emails.clear()
    A._send_overdue_loaner_reminders()
    assert [to for to, _, _ in sent_emails] == [other.email]

    sent_emails.clear()
    r = client.post('/admin/loaners/email_selected', data={'asset_tags': [for_repair.asset_tag, plain.asset_tag]},
                    follow_redirects=True)
    assert [to for to, _, _ in sent_emails] == [other.email] and b'repair loaners' in r.data

    from foxdesk.automation.actions import _recipient
    loaner_row = A.LoanerCheckout.query.filter_by(asset_tag=for_repair.asset_tag).one()
    assert _recipient(dict(loaner=loaner_row, holder=kid, person=kid), 'holder', '') is None
    assert _recipient(dict(loaner=loaner_row, holder=kid, person=kid), 'address', 'techs@district.org') == 'techs@district.org'


def test_site_form_techs_and_bells(client, make):
    site = make.site('Middle')
    tech = make_user('mstech', 'mstech@district.org')
    r = client.post(f'/admin/sites/{site.id}/edit', data={'name': 'Middle', 'tech_user_id': [str(tech.id)],
                                                          'bell_schedule': '1, 7:55, 8:48\nnonsense line'},
                    follow_redirects=True)
    assert b'aren&#39;t &#34;period, start, end&#34;' in r.data or b"aren't" in r.data
    assert A.db.session.get(A.Site, site.id).techs == []
    client.post(f'/admin/sites/{site.id}/edit', data={'name': 'Middle', 'tech_user_id': [str(tech.id)],
                                                      'bell_schedule': '1, 7:55, 8:48\n2, 8:52, 9:45'})
    site = A.db.session.get(A.Site, site.id)
    assert [u.username for u in site.techs] == ['mstech'] and site.bell_schedule.startswith('1, 7:55')
    assert 'mstech' in client.get(f'/admin/sites/{site.id}/edit').get_data(as_text=True)


def test_send_mine_needs_a_user_account(client):
    r = client.post('/admin/overdue/send_mine', follow_redirects=True)
    assert r.status_code == 200
    assert client.get('/admin/overdue').status_code == 200


def test_settings_validation(client):
    r = client.post('/admin/overdue/settings', data={'repair_max_days': '0', 'alert_hour': '7'}, follow_redirects=True)
    assert b'1\xe2\x80\x93120 days' in r.data
    client.post('/admin/overdue/settings', data={'repair_max_days': '10', 'alert_hour': '6'})
    row = od.overdue_settings()
    assert (row.repair_max_days, row.alert_hour, row.alerts_enabled) == (10, 6, False)


def test_user_form_offers_the_people_email(client, make):
    tech = make_user('mstech')
    make.person('Ms', 'Tech', role='staff', email='mstech@district.org')
    make.person('Ms', 'Student', email='mstech@students.org')
    site = make.site('Middle')
    site.techs = [tech]
    A.db.session.commit()
    form = client.get(f'/admin/users/{tech.id}/edit').get_data(as_text=True)
    assert 'Use mstech@district.org' in form and 'mstech@students.org' not in form, 'staff People records only'
    assert f'href="/admin/users/{tech.id}/edit"' in client.get(f'/admin/sites/{site.id}/edit').get_data(as_text=True)
    tech.email = 'mstech@district.org'
    A.db.session.commit()
    assert 'Use mstech@district.org' not in client.get(f'/admin/users/{tech.id}/edit').get_data(as_text=True)
