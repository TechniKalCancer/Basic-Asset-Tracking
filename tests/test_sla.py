"""SLA targets in school hours, the Past SLA queue, the missed-SLA automation, and help desk reports."""
from datetime import datetime, timedelta

from flask import g

from conftest import A
from foxdesk.clock import utcnow

# Friday Oct 9 2026, 3:50 PM Eastern (EDT, UTC-4).
FRIDAY_350PM = datetime(2026, 10, 9, 19, 50)


def sla_on(client, closed='', **targets):
    form = {'enabled': 'on', 'work_days': ['0', '1', '2', '3', '4'], 'day_start': '07:30', 'day_end': '16:00',
            'closed_dates': closed}
    for p in A.TICKET_PRIORITIES:
        form[f'{p}_response'], form[f'{p}_resolve'] = targets.get(p, ('8', '40'))
    r = client.post('/admin/sla', data=form, follow_redirects=True)
    g.pop('_sla', None)
    return r


def new_ticket(cat, **kw):
    t = A._create_ticket(cat.id, kw.pop('subject', 'Projector'), 'It flickers.', requester_name='Ana Ruiz',
                         requester_email='ana@example.edu', **kw)
    A.db.session.commit()
    return t


def test_school_hours_calendar():
    row = A.SlaSettings(work_days='0,1,2,3,4', day_start='07:30', day_end='16:00', closed_dates='')
    cal = A.Calendar(row)
    assert cal.add_hours(FRIDAY_350PM, 1) == datetime(2026, 10, 12, 12, 20), 'Fri 3:50 PM + 1 school hour = Mon 8:20 AM'
    assert cal.minutes_between(FRIDAY_350PM, datetime(2026, 10, 12, 12, 20)) == 60
    row.closed_dates = '2026-10-12  # teacher work day'
    assert A.Calendar(row).add_hours(FRIDAY_350PM, 1) == datetime(2026, 10, 13, 12, 20), 'closed days are skipped'
    days, problems = A.parse_closed_dates('2026-12-21 to 2027-01-01\n2026-11-26\nnot a date')
    assert len(days) == 13 and problems == ['not a date']


def test_settings_set_due_times_and_validate(client, make):
    cat = make.ticket_category()
    before = new_ticket(cat)
    assert before.response_due_at is None, 'no clocks while SLAs are off'
    r = sla_on(client, urgent=('1', '8'))
    assert b'Clocks start with the next ticket' in r.data
    assert A.db.session.get(A.Ticket, before.id).response_due_at is None, 'tickets already open don\'t become overdue'
    urgent = new_ticket(cat, priority='urgent')
    assert urgent.resolve_due_at > urgent.response_due_at
    first_due = urgent.response_due_at
    r = sla_on(client, urgent=('2', '8'))
    assert b'Due times updated on 1 open ticket' in r.data
    assert A.db.session.get(A.Ticket, urgent.id).response_due_at > first_due, 'new targets move open clocks'

    r = sla_on(client, closed='someday', urgent=('9', '1'))
    assert b'couldn&#39;t read &#34;someday&#34;' in r.data or b'someday' in r.data
    assert b'can&#39;t be shorter' in r.data
    assert A.SlaSettings.query.one().targets['urgent']['response'] == 2, 'a bad form changes nothing'


def test_reply_stops_the_clock_and_priority_moves_it(client, make, sent_emails):
    cat = make.ticket_category()
    sla_on(client, low=('16', '80'), urgent=('1', '8'))
    t = new_ticket(cat, priority='low')
    low_due = t.response_due_at
    client.post(f'/admin/tickets/{t.id}/status', data={'priority': 'urgent'})
    t = A.db.session.get(A.Ticket, t.id)
    assert t.response_due_at < low_due, 'urgent is due sooner'
    client.post(f'/admin/tickets/{t.id}/comment', data={'body': 'Internal note'})
    assert A.db.session.get(A.Ticket, t.id).first_response_at is None, 'an internal note isn\'t a reply'
    client.post(f'/admin/tickets/{t.id}/comment', data={'body': 'On my way', 'email_requester': 'on'})
    assert A.db.session.get(A.Ticket, t.id).first_response_at is not None


def test_past_sla_queue_and_badges(client, make):
    cat = make.ticket_category()
    sla_on(client)
    late = new_ticket(cat, subject='SUBJ-late')
    fine = new_ticket(cat, subject='SUBJ-fine')
    late.response_due_at = utcnow() - timedelta(minutes=5)
    A.db.session.commit()
    page = client.get('/admin/tickets?view=overdue').get_data(as_text=True)
    assert 'Past SLA' in page and 'SUBJ-late' in page and 'SUBJ-fine' not in page
    assert 'Reply overdue' in page
    detail = client.get(f'/admin/tickets/{fine.id}').get_data(as_text=True)
    assert 'Reply by' in detail or 'Reply due soon' in detail


def test_missed_sla_automation_fires_once(client, make, sent_emails):
    cat = make.ticket_category()
    sla_on(client)
    t = new_ticket(cat)
    rule = A.AutomationRule(name='Tell the lead', trigger='ticket.sla_breached', enabled=True,
                            conditions=[{'field': 'ticket.sla_breach', 'op': 'eq', 'value': 'response'}],
                            actions=[{'type': 'add_comment', 'params': {'body': 'Missed its {sla_missed} time'}}])
    A.db.session.add(rule)
    t.response_due_at = utcnow() - timedelta(minutes=1)
    A.db.session.commit()
    assert A.run_scheduled() == 1
    assert A.run_scheduled() == 0, 'once per ticket per missed clock'
    assert any(c.body == 'Missed its reply time' for c in A.db.session.get(A.Ticket, t.id).comments)
    assert 'sla_missed_email' in A.TEMPLATES_BY_KEY


def test_helpdesk_reports(client, make):
    cat = make.ticket_category('Network')
    sla_on(client)
    now = utcnow()
    quick = new_ticket(cat, subject='Quick')
    quick.first_response_at = quick.created_at + timedelta(minutes=30)
    quick.status, quick.resolved_at = 'resolved', quick.created_at + timedelta(hours=2)
    new_ticket(cat, subject='Still open')
    dupe = new_ticket(cat, subject='Dupe')
    dupe.merged_into_id, dupe.status = quick.id, 'closed'
    A.db.session.commit()
    report = A.build_helpdesk_report(None, '30', now=now + timedelta(minutes=1))
    assert (report['totals']['opened'], report['totals']['resolved'], report['totals']['open_now']) == (2, 1, 1), \
        'merged tickets are left out'
    assert [r['label'] for r in report['by_category']] == ['Network']
    page = client.get('/admin/helpdesk_reports?range=90').get_data(as_text=True)
    assert 'Median first reply' in page and 'Open tickets at the end of each week' in page
    csv_text = client.get('/admin/helpdesk_reports/export').get_data(as_text=True)
    assert 'Quick' in csv_text and 'Dupe' not in csv_text
