"""Automation rules: conditions, event and scheduled triggers, the builder, templates, legacy conversion."""
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from flask import g
from flask_migrate import downgrade, upgrade

from conftest import A, ROOT


def rule(trigger, conditions=(), actions=(), **kw):
    r = A.AutomationRule(name=kw.pop('name', 'Test rule'), trigger=trigger, conditions=list(conditions),
                         actions=list(actions), enabled=kw.pop('enabled', True), **kw)
    A.db.session.add(r)
    A.db.session.commit()
    return r


def c(field, op, value=None):
    return {'field': field, 'op': op, 'value': value}


def a(kind, **params):
    return {'type': kind, 'params': params}


def new_ticket(client, cat, subject='Help', person=None, asset_tag='', priority='normal'):
    client.post('/admin/tickets/new', content_type='multipart/form-data',
                data={'person_id': str(person.id) if person else '', 'requester_name': 'Walk Up',
                      'category_id': str(cat.id), 'subject': subject, 'description': 'd', 'priority': priority,
                      'site_id': '', 'asset_tag': asset_tag})
    return A.Ticket.query.order_by(A.Ticket.id.desc()).first()


# ─── conditions ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('cond, facts, expected', [
    (c('repair.days_out', 'gte', '14'), {'repair.days_out': 20}, True),
    (c('repair.days_out', 'gte', '14'), {'repair.days_out': 3}, False),
    (c('repair.days_out', 'lt', '14'), {'repair.days_out': None}, False),
    (c('ticket.subject', 'contains', 'SCREEN'), {'ticket.subject': 'Cracked screen'}, True),
    (c('ticket.subject', 'not_contains', 'screen'), {'ticket.subject': 'Cracked screen'}, False),
    (c('ticket.subject', 'starts', 'crack'), {'ticket.subject': 'Cracked screen'}, True),
    (c('ticket.category', 'eq', '4'), {'ticket.category': 4}, True),
    (c('ticket.category', 'in', ['2', '4']), {'ticket.category': 4}, True),
    (c('ticket.category', 'in', '2, 5'), {'ticket.category': 4}, False),
    (c('ticket.has_device', 'true'), {'ticket.has_device': True}, True),
    (c('person.has_protection_plan', 'false'), {'person.has_protection_plan': False}, True),
    (c('ticket.assignee', 'empty'), {'ticket.assignee': None}, True),
    (c('ticket.assignee', 'not_empty'), {}, False),
])
def test_conditions(cond, facts, expected):
    assert A.check_condition(cond, facts)[0] is expected


def test_match_all_vs_any():
    r = A.AutomationRule(conditions=[c('ticket.priority', 'eq', 'urgent'), c('ticket.subject', 'contains', 'x')])
    facts = {'ticket.priority': 'urgent', 'ticket.subject': 'nothing'}
    r.match = 'all'
    assert A.evaluate(r, facts)[0] is False
    r.match = 'any'
    assert A.evaluate(r, facts)[0] is True


# ─── event triggers ───────────────────────────────────────────────────────────

def test_ticket_rule_runs_and_is_logged(client, make):
    cat = make.ticket_category()
    r = rule('ticket.created', [c('ticket.subject', 'contains', 'screen')],
             [a('set_priority', value='high'), a('add_comment', body='Auto: {ticket_subject} ({ticket_id})')])
    plain = new_ticket(client, cat, 'Will not charge')
    hit = new_ticket(client, cat, 'Broken screen')
    assert plain.priority == 'normal' and hit.priority == 'high'
    assert A.TicketComment.query.filter_by(ticket_id=hit.id).one().body == f'Auto: Broken screen ({hit.id})'
    run = A.AutomationRun.query.one()
    assert run.status == 'ran' and run.subject_label.startswith(f'Ticket #{hit.id}')
    assert A.AutomationRule.query.get(r.id).run_count == 1
    assert A.ActivityLog.query.filter_by(action='automation_run', ticket_id=hit.id).count() == 1


def test_site_filter(client, make):
    cat, north, south = make.ticket_category(), make.site('North'), make.site('South')
    rule('ticket.created', [], [a('set_priority', value='urgent')], site_id=north.id)
    client.post('/admin/tickets/new', content_type='multipart/form-data',
                data={'requester_name': 'X', 'category_id': str(cat.id), 'subject': 's', 'description': 'd',
                      'priority': 'normal', 'site_id': str(south.id)})
    assert A.Ticket.query.one().priority == 'normal'


def test_paused_rule_and_feature_switch(client, make):
    cat = make.ticket_category()
    r = rule('ticket.created', [], [a('set_priority', value='urgent')], enabled=False)
    assert new_ticket(client, cat).priority == 'normal'
    r.enabled = True
    A.set_feature('automations', False, 'test')
    A.db.session.commit()
    g.pop('_feature_overrides', None)
    assert new_ticket(client, cat).priority == 'normal'
    assert A.AutomationRun.query.count() == 0


def test_failing_action_does_not_stop_the_rest(client, make, monkeypatch):
    def boom(ctx, p, dry_run):
        raise RuntimeError('kaboom')
    monkeypatch.setitem(A.ACTIONS['add_comment'], 'run', boom)
    cat = make.ticket_category()
    rule('ticket.created', [], [a('add_comment', body='x'), a('set_priority', value='high')])
    t = new_ticket(client, cat)
    assert t.priority == 'high', 'the ticket was still created and the next action still ran'
    run = A.AutomationRun.query.one()
    assert run.status == 'error' and 'kaboom' in run.results[0]['message']


def test_staged_device_action_uses_rule_settings(client, make):
    cat = make.ticket_category()
    screen = A.RepairCategory(name='Screen')
    A.db.session.add(screen)
    A.db.session.commit()
    dev = make.device(holder=make.person())
    r = rule('ticket.created', [c('ticket.has_device', 'true')],
             [a('send_to_repair', repair_category=str(screen.id), confirm=True)])
    t = new_ticket(client, cat, asset_tag=dev.asset_tag)
    pending = A.PendingDeviceAction.query.one()
    assert (pending.rule_id, pending.ticket_id, pending.status) == (r.id, t.id, 'pending')
    assert A.Repair.query.count() == 0, 'nothing happens until a tech confirms'

    body = client.get(f'/admin/rules/{r.id}').get_data(as_text=True)
    assert 'Waiting for a tech to confirm' in body and dev.asset_tag in body
    client.post(f'/admin/pending_actions/{pending.id}/confirm')
    assert A.Repair.query.one().repair_category_id == screen.id


def test_damage_escalation_template(client, make, sent_emails):
    kid = make.person(guardian_name='Pat Parent', guardian_email='parent@example.com')
    dev = make.device(holder=kid)
    built = A.TEMPLATES_BY_KEY['damage_escalation']['build']()
    rule(built['trigger'], built['conditions'], built['actions'])
    client.post(f'/admin/assets/{dev.asset_tag}/incidents', data={'description': 'Dropped it'})
    first = A.Incident.query.one()
    assert not first.fee_charged and not sent_emails, 'the first report is free'
    client.post(f'/admin/assets/{dev.asset_tag}/incidents', data={'description': 'Dropped it again'})
    second = A.Incident.query.order_by(A.Incident.id.desc()).first()
    assert second.fee_charged and second.fee_amount == Decimal('45.00')
    assert sent_emails[-1][0] == 'parent@example.com' and '$45.00' in sent_emails[-1][2]


# ─── scheduled triggers ───────────────────────────────────────────────────────

def test_scheduled_rule_fires_once_per_subject(make, sent_emails):
    late, recent = make.device(), make.device()
    now = datetime.utcnow()
    rep = A.Repair(asset_tag=late.asset_tag, issue_description='Screen', sent_at=now - timedelta(days=20))
    A.db.session.add_all([rep, A.Repair(asset_tag=recent.asset_tag, issue_description='Key', sent_at=now - timedelta(days=3))])
    A.db.session.commit()
    rule('repair.overdue', [c('repair.days_out', 'gte', '14')],
         [a('send_email', to='address', address='techs@example.edu', subject='{asset_tag} out {repair_days_out} days')])
    assert A.run_scheduled() == 1
    assert A.run_scheduled() == 0, 'a second check (or a second worker) must not fire again'
    assert sent_emails == [('techs@example.edu', f'{late.asset_tag} out 20 days', '')]
    assert A.AutomationRun.query.one().dedupe_key == f'repair:{rep.id}'


# ─── builder pages ────────────────────────────────────────────────────────────

def test_builder_saves_only_known_fields_and_actions(client, make):
    make.ticket_category()
    r = client.post('/admin/rules/new', data={
        'name': 'Urgent to in progress', 'trigger': 'ticket.created', 'match': 'all', 'enabled': 'on',
        'conditions_json': json.dumps([c('ticket.priority', 'eq', 'urgent'), c('nope', 'eq', 'x')]),
        'actions_json': json.dumps([a('set_status', value='in_progress'), a('format_c_drive')]),
    })
    saved = A.AutomationRule.query.one()
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/admin/rules/{saved.id}')
    assert saved.conditions == [c('ticket.priority', 'eq', 'urgent')]
    assert [x['type'] for x in saved.actions] == ['set_status'] and saved.created_by
    body = client.get(f'/admin/rules/{saved.id}').get_data(as_text=True)
    assert 'Ticket priority is Urgent' in body and 'Set ticket status' in body


@pytest.mark.parametrize('form, message', [
    ({'name': '', 'actions_json': json.dumps([a('set_priority', value='high')])}, 'Give the rule a name'),
    ({'name': 'x', 'actions_json': '[]'}, 'Add at least one action'),
    ({'name': 'x', 'actions_json': '{not json'}, 'couldn&#39;t be read'),
    ({'name': 'x', 'actions_json': json.dumps([a('post_webhook', url='http://example.com/hook')])}, 'https://'),
    ({'name': 'x', 'actions_json': json.dumps([a('send_email', to='address', address='')])}, 'email address'),
])
def test_builder_rejects_bad_rules(client, form, message):
    r = client.post('/admin/rules/new', data=dict({'trigger': 'ticket.created'}, **form))
    assert r.status_code == 200 and message in r.get_data(as_text=True)
    assert A.AutomationRule.query.count() == 0


def test_every_template_is_valid_and_opens_prefilled(client, make):
    make.ticket_category('Cryptohome Error')
    for t in A.TEMPLATES:
        built = t['build']()
        trigger = A.TRIGGERS[built['trigger']]
        for cond in built['conditions']:
            assert cond['field'] in trigger['fields'], (t['key'], cond)
            assert cond['op'] in {op for op, _ in A.OPERATORS[A.FIELDS[cond['field']][1]]}, (t['key'], cond)
        for step in built['actions']:
            spec = A.ACTIONS[step['type']]
            assert spec['subjects'] == '*' or trigger['subject'] in spec['subjects'], (t['key'], step)
            assert set(step['params']) <= {p[0] for p in spec['params']}, (t['key'], step)
        r = client.get(f'/admin/rules/new?template={t["key"]}')
        assert r.status_code == 200, t['key']
    assert client.get('/admin/rules/new?template=nope').status_code == 404
    body = client.get('/admin/rules').get_data(as_text=True)
    assert 'Second damage report' in body


def test_dry_run_changes_nothing(client, make):
    cat = make.ticket_category()
    t = new_ticket(client, cat, 'Cracked screen')
    r = rule('ticket.created', [c('ticket.subject', 'contains', 'screen')], [a('set_priority', value='urgent')])
    body = client.post(f'/admin/rules/{r.id}/test').get_data(as_text=True)
    assert 'Would set Priority to urgent' in body and f'Ticket #{t.id}' in body
    assert A.Ticket.query.get(t.id).priority == 'normal' and A.AutomationRun.query.count() == 0


def test_toggle_and_delete_keep_staged_actions(client, make):
    r = rule('ticket.created', [], [a('set_priority', value='high')])
    A.db.session.add(A.PendingDeviceAction(asset_tag='T1', action_type='profile_clear', status='pending', rule_id=r.id))
    A.db.session.add(A.AutomationRun(rule_id=r.id, trigger='ticket.created', status='ran'))
    A.db.session.commit()
    client.post(f'/admin/rules/{r.id}/toggle')
    assert A.AutomationRule.query.get(r.id).enabled is False
    client.post(f'/admin/rules/{r.id}/delete')
    assert A.AutomationRule.query.count() == 0 and A.AutomationRun.query.count() == 0
    assert A.PendingDeviceAction.query.one().rule_id is None


def test_legacy_automations_page_redirects(client):
    r = client.get('/admin/automations')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/rules')


# ─── upgrade from the old ticket automations ──────────────────────────────────

def test_migration_converts_legacy_ticket_automations(client, make):
    cat = make.ticket_category('Cracked Screen')
    screen = A.RepairCategory(name='Screen')
    A.db.session.add(screen)
    A.db.session.commit()
    A.db.session.add(A.TicketAutomation(ticket_category_id=cat.id, action_type='send_to_repair',
                                        require_confirmation=False, is_active=True, repair_category_id=screen.id))
    A.db.session.commit()
    cat_id, screen_id = cat.id, screen.id
    A.db.session.remove()

    migrations = os.path.join(ROOT, 'migrations')
    downgrade(directory=migrations, revision='4a5368f32367')
    upgrade(directory=migrations)

    converted = A.AutomationRule.query.one()
    assert converted.trigger == 'ticket.created' and converted.enabled
    assert converted.conditions == [c('ticket.category', 'eq', str(cat_id)), c('ticket.has_device', 'true')]
    assert converted.actions == [a('send_to_repair', confirm=False, repair_category=str(screen_id))]
    assert 'Cracked Screen tickets' in converted.name

    other = make.ticket_category('Other')
    dev = make.device(holder=make.person())
    new_ticket(client, other, asset_tag=dev.asset_tag)
    assert A.Repair.query.count() == 0, 'other categories are untouched'
    new_ticket(client, A.TicketCategory.query.get(cat_id), asset_tag=dev.asset_tag)
    assert A.Repair.query.one().repair_category_id == screen_id, 'the converted rule behaves like the old automation'
