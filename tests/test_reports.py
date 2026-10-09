"""Scheduled report emails and low-stock alerts, both as automation rules."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from conftest import A, patch_everywhere

ET = ZoneInfo('America/New_York')


def at(monkeypatch, *args):
    """Pretend it's this local (district) time."""
    when = datetime(*args, tzinfo=ET)
    patch_everywhere(monkeypatch, 'local_now', lambda: when)
    return when


def rule(trigger, conditions, actions, **kw):
    r = A.AutomationRule(name=kw.pop('name', 'Report'), trigger=trigger, conditions=conditions, actions=actions, enabled=True, **kw)
    A.db.session.add(r)
    A.db.session.commit()
    return r


def test_periods_follow_district_time():
    monday_8am = datetime(2026, 10, 12, 8, 0, tzinfo=ET)
    week = A.current_period('weekly', monday_8am)
    assert week['label'] == 'the week of October 5' and week['key'] == 'week:2026-10-12'
    assert week['start'] == datetime(2026, 10, 5, 4, 0) and week['end'] == datetime(2026, 10, 12, 4, 0), 'midnight ET in UTC'
    assert A.current_period('daily', monday_8am)['label'] == 'Sunday, October 11'
    month = A.current_period('monthly', datetime(2026, 11, 1, 7, 30, tzinfo=ET))
    assert month['label'] == 'October 2026' and month['start'] == datetime(2026, 10, 1, 4, 0)


def test_weekly_summary_sends_once_a_week(make, monkeypatch, sent_emails):
    north, south = make.site('North'), make.site('South')
    cat = make.ticket_category()
    for site in (north, north, south):
        A.db.session.add(A.Ticket(category_id=cat.id, subject='Help', description='d', site_id=site.id,
                                  created_at=datetime(2026, 10, 7, 15, 0)))
    A.db.session.commit()
    built = A.TEMPLATES_BY_KEY['weekly_summary']['build']()
    built['actions'][0]['params']['to'] = 'principal@north.edu, office@north.edu'
    rule(built['trigger'], built['conditions'], built['actions'], site_id=north.id)

    at(monkeypatch, 2026, 10, 12, 6, 45)
    assert A.run_scheduled() == 0 and not sent_emails, 'not before 7'
    at(monkeypatch, 2026, 10, 12, 7, 15)
    assert A.run_scheduled() == 1
    assert sorted(to for to, _, _ in sent_emails) == ['office@north.edu', 'principal@north.edu']
    subject, body = sent_emails[0][1], sent_emails[0][2]
    assert 'the week of October 5' in subject and 'North' in subject
    assert 'Opened: 2' in body, 'only the rule\'s school is counted'
    at(monkeypatch, 2026, 10, 12, 9, 0)
    assert A.run_scheduled() == 0, 'once per week'
    at(monkeypatch, 2026, 10, 14, 9, 0)
    assert A.run_scheduled() == 0, 'Wednesday is not Monday'
    at(monkeypatch, 2026, 10, 19, 7, 5)
    assert A.run_scheduled() == 1, 'next Monday'


def test_every_report_builds(make):
    kid = make.person()
    dev = make.device(holder=kid)
    cat = make.ticket_category()
    now = datetime.utcnow()
    A.db.session.add_all([
        A.Ticket(category_id=cat.id, subject='Cracked', description='d', priority='urgent'),
        A.Incident(asset_tag=dev.asset_tag, person_name=kid.full_name, description='Dropped', fee_charged=True,
                   fee_amount=Decimal('45.00'), created_at=now - timedelta(days=2)),
        A.Repair(asset_tag=dev.asset_tag, issue_description='Screen', sent_at=now - timedelta(days=20)),
        A.LoanerCheckout(asset_tag=dev.asset_tag, person_name=kid.full_name, due_date=(now - timedelta(days=3)).date()),
        A.Part(name='Battery', category='battery', quantity_on_hand=1, reorder_level=2),
    ])
    A.db.session.commit()
    start, end = now - timedelta(days=7), now
    texts = {k: A.build_report(k, start, end, None, 'last week')[1] for k in A.REPORTS}
    assert '1 urgent' in texts['summary'] and '$45.00' in texts['summary'] and 'Battery: 1 left' in texts['summary']
    assert 'OVERDUE LOANERS (1)' in texts['overdue'] and '3 days overdue' in texts['overdue'] and '20 days' in texts['overdue']
    assert '[urgent] Cracked' in texts['tickets']
    assert 'Dropped' in texts['damage'] and 'UNPAID FEES (1, $45.00)' in texts['damage']


def test_report_rule_needs_an_address_and_dry_runs(client, monkeypatch):
    built = A.TEMPLATES_BY_KEY['daily_overdue']['build']()
    import json
    r = client.post('/admin/rules/new', data={'name': 'Overdue', 'trigger': built['trigger'], 'enabled': 'on',
                                              'conditions_json': json.dumps(built['conditions']),
                                              'actions_json': json.dumps(built['actions'])})
    assert b'email address(es) the report goes to' in r.data
    built['actions'][0]['params']['to'] = 'techs@example.edu'
    client.post('/admin/rules/new', data={'name': 'Overdue', 'trigger': built['trigger'], 'enabled': 'on',
                                          'conditions_json': json.dumps(built['conditions']),
                                          'actions_json': json.dumps(built['actions'])})
    saved = A.AutomationRule.query.one()
    at(monkeypatch, 2026, 10, 13, 8, 0)
    body = client.post(f'/admin/rules/{saved.id}/test').get_data(as_text=True)
    assert 'Would email' in body and 'techs@example.edu' in body and A.AutomationRun.query.count() == 0


def test_low_stock_alert_once_per_restock(client, monkeypatch, sent_emails):
    part = A.Part(name='Battery', category='battery', quantity_on_hand=2, reorder_level=2)
    A.db.session.add(part)
    A.db.session.commit()
    built = A.TEMPLATES_BY_KEY['part_low_email']['build']()
    built['actions'][0]['params']['address'] = 'orders@example.edu'
    rule(built['trigger'], built['conditions'], built['actions'])
    assert A.run_scheduled() == 1 and sent_emails[-1][1] == 'Reorder: Battery (2 left)'
    assert A.run_scheduled() == 0, 'not again until it is restocked'
    client.post(f'/admin/parts/{part.id}/stock', data={'action': 'received', 'quantity': '5'})
    assert A.run_scheduled() == 0, 'not low any more'
    client.post(f'/admin/parts/{part.id}/stock', data={'action': 'count', 'counted': '1'})
    assert A.run_scheduled() == 1 and sent_emails[-1][1] == 'Reorder: Battery (1 left)'
