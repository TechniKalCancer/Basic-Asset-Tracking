"""Pages: automation rules (Settings → Automations)."""
import json

from flask import abort, flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import AUTOMATION_ACTIONS, AutomationRule, AutomationRun, PendingDeviceAction, Site
from foxdesk.automation.actions import ACTIONS, actions_for
from foxdesk.automation.engine import test_rule
from foxdesk.automation.templates import TEMPLATES, TEMPLATES_BY_KEY
from foxdesk.automation.triggers import FIELDS, OPERATORS, PLACEHOLDER_NAMES, TRIGGER_GROUPS, TRIGGERS
from foxdesk.services.auth import _current_actor, _log_activity, require_super_admin
from foxdesk.services.features import feature_enabled

OP_LABELS = {op: label for ops in OPERATORS.values() for op, label in ops}
NO_VALUE_OPS = {'empty', 'not_empty', 'true', 'false'}


def _available_triggers():
    return {k: t for k, t in TRIGGERS.items() if not t.get('feature') or feature_enabled(t['feature'])}


class _Choices:
    """Per-request cache of the prefilled choice lists — the same category
    or staff list is looked up once, not once per condition row."""
    def __init__(self):
        self._cache = {}

    def __call__(self, fn):
        if fn is None:
            return None
        if fn not in self._cache:
            self._cache[fn] = [[str(v), str(label)] for v, label in fn()]
        return self._cache[fn]

    def label(self, fn, value):
        for v, label in self(fn) or []:
            if v == str(value):
                return label
        return str(value)


def _builder_schema():
    """Everything the rule builder's JavaScript needs, with every dropdown
    prefilled from the district's own data (categories, sites, staff ...)."""
    choices = _Choices()
    triggers = {}
    for key, t in _available_triggers().items():
        triggers[key] = dict(label=t['label'], group=t['group'], scheduled=bool(t.get('scheduled')), hint=t.get('hint'),
                             fields=[f for f in t['fields'] if f in FIELDS],
                             actions=list(actions_for(t['subject'])))
    fields = {k: dict(label=v[0], type=v[1], choices=choices(v[2])) for k, v in FIELDS.items()}
    actions = {k: dict(label=a['label'],
                       params=[dict(key=p[0], label=p[1], type=p[2], choices=choices(p[3]), default=p[4])
                               for p in a['params']])
               for k, a in ACTIONS.items()}
    return dict(triggers=triggers, fields=fields, actions=actions,
                operators={t: [list(o) for o in ops] for t, ops in OPERATORS.items()},
                placeholders=PLACEHOLDER_NAMES)


def _describe_rule(rule, choices):
    """The rule in plain words, for the list and detail pages."""
    trigger = TRIGGERS.get(rule.trigger)
    conditions = []
    for c in rule.conditions or []:
        label, ftype, fn = FIELDS.get(c.get('field'), (c.get('field'), 'text', None))
        if ftype == 'bool':
            conditions.append(f'{label}: {"yes" if c.get("op") == "true" else "no"}')
            continue
        op_label = dict(OPERATORS.get(ftype, ())).get(c.get('op')) or OP_LABELS.get(c.get('op'), c.get('op'))
        text = f'{label} {op_label}'
        if c.get('op') not in NO_VALUE_OPS:
            value = c.get('value')
            values = value if isinstance(value, list) else [value]
            text += ' ' + ', '.join(choices.label(fn, v) if fn else f'"{v}"' if ftype == 'text' else str(v)
                                    for v in values)
        conditions.append(text)
    steps = []
    for step in rule.actions or []:
        spec = ACTIONS.get(step.get('type'))
        if not spec:
            steps.append((step.get('type'), ['No longer available'], True))
            continue
        params = step.get('params') or {}
        details = []
        for key, label, ptype, fn, _ in spec['params']:
            value = params.get(key)
            if ptype == 'bool':
                if key == 'confirm':
                    details.append('waits for a tech to confirm' if value else 'runs right away')
                elif value:
                    details.append(label.lower())
            elif value not in (None, ''):
                shown = choices.label(fn, value) if fn else str(value)
                details.append(f'{label}: {shown[:80]}{"…" if len(shown) > 80 else ""}')
        steps.append((spec['label'], details, bool(spec.get('feature')) and not feature_enabled(spec['feature'])))
    return dict(when=trigger['label'] if trigger else f'Unknown trigger "{rule.trigger}"',
                scheduled=bool(trigger and trigger.get('scheduled')),
                trigger_off=bool(trigger and trigger.get('feature') and not feature_enabled(trigger['feature'])),
                match='any' if rule.match == 'any' else 'all', conditions=conditions, steps=steps)


def _rule_from_form(rule):
    """Fill `rule` from the builder's POST. The builder serializes conditions
    and actions as JSON so the server never has to parse dynamic row names."""
    rule.name = request.form.get('name', '').strip()[:160]
    rule.description = request.form.get('description', '').strip() or None
    rule.trigger = request.form.get('trigger', '')
    rule.match = 'any' if request.form.get('match') == 'any' else 'all'
    rule.site_id = request.form.get('site_id', type=int) or None
    rule.enabled = request.form.get('enabled') == 'on'
    try:
        conditions = json.loads(request.form.get('conditions_json') or '[]')
        actions = json.loads(request.form.get('actions_json') or '[]')
    except ValueError:
        raise ValueError('The rule couldn\'t be read. Please try again.')
    if not isinstance(conditions, list) or not isinstance(actions, list):
        raise ValueError('The rule couldn\'t be read. Please try again.')
    rule.conditions = [dict(field=c['field'], op=c['op'], value=c.get('value')) for c in conditions
                       if isinstance(c, dict) and c.get('field') in FIELDS and c.get('op') in OP_LABELS]
    rule.actions = [dict(type=a['type'], params=a.get('params') if isinstance(a.get('params'), dict) else {})
                    for a in actions if isinstance(a, dict) and a.get('type') in ACTIONS]
    if not rule.name:
        raise ValueError('Give the rule a name.')
    if rule.trigger not in _available_triggers():
        raise ValueError('Choose what starts the rule.')
    if not rule.actions:
        raise ValueError('Add at least one action.')
    for step in rule.actions:
        url = (step['params'].get('url') or '').strip()
        if step['type'] == 'post_webhook' and not url.startswith('https://'):
            raise ValueError('The webhook URL must start with https://')
        if step['type'] == 'send_report' and '@' not in (step['params'].get('to') or ''):
            raise ValueError('Enter the email address(es) the report goes to.')
        if step['type'] == 'send_email' and step['params'].get('to') == 'address' \
                and '@' not in (step['params'].get('address') or ''):
            raise ValueError('Enter the email address the rule should send to.')


def _template_cards():
    """Template gallery, grouped, leaving out ones whose trigger or every
    action belongs to a module that's turned off."""
    groups = {}
    for t in TEMPLATES:
        built = t['build']()
        trigger = TRIGGERS[built['trigger']]
        if trigger.get('feature') and not feature_enabled(trigger['feature']):
            continue
        usable = actions_for(trigger['subject'])
        if not any(a['type'] in usable for a in built['actions']):
            continue
        groups.setdefault(t['group'], []).append(dict(t, when=trigger['label']))
    return groups


@app.route('/admin/rules')
@require_super_admin
def admin_rules():
    rules = AutomationRule.query.order_by(AutomationRule.enabled.desc(), AutomationRule.name).all()
    choices = _Choices()
    return render_template('admin_rules.html', rules=rules,
                           summaries={r.id: _describe_rule(r, choices) for r in rules},
                           template_groups=_template_cards(),
                           used_templates={r.template_key for r in rules if r.template_key})


@app.route('/admin/rules/new', methods=['GET', 'POST'])
@require_super_admin
def admin_rule_new():
    rule = AutomationRule(enabled=True, match='all', conditions=[], actions=[])
    template_key = request.args.get('template') or request.form.get('template_key')
    if request.method == 'GET' and template_key:
        tpl = TEMPLATES_BY_KEY.get(template_key) or abort(404)
        built = tpl['build']()
        rule.name, rule.description, rule.template_key = tpl['name'], tpl['description'], template_key
        rule.trigger, rule.match = built['trigger'], built.get('match', 'all')
        rule.conditions, rule.actions = built['conditions'], built['actions']
    if request.method == 'POST':
        try:
            _rule_from_form(rule)
            rule.template_key = template_key if template_key in TEMPLATES_BY_KEY else None
            rule.created_by = _current_actor()[1]
            db.session.add(rule)
            _log_activity('automation_rule', f'Created automation "{rule.name}".')
            db.session.commit()
            when = ' It checks every 15 minutes.' if TRIGGERS[rule.trigger].get('scheduled') else ''
            flash(f'Saved "{rule.name}".{when} Use Test to see what it would do.', 'success')
            return redirect(url_for('admin_rule_detail', rule_id=rule.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
    return _render_form(rule)


@app.route('/admin/rules/<int:rule_id>/edit', methods=['GET', 'POST'])
@require_super_admin
def admin_rule_edit(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    if request.method == 'POST':
        try:
            _rule_from_form(rule)
            _log_activity('automation_rule', f'Edited automation "{rule.name}".')
            db.session.commit()
            flash(f'Saved "{rule.name}".', 'success')
            return redirect(url_for('admin_rule_detail', rule_id=rule.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
    return _render_form(rule)


def _render_form(rule):
    initial = dict(trigger=rule.trigger or '', match=rule.match or 'all',
                   conditions=rule.conditions or [], actions=rule.actions or [])
    return render_template('admin_rule_form.html', rule=rule, schema=_builder_schema(), initial=initial,
                           trigger_groups=TRIGGER_GROUPS, sites=Site.query.order_by(Site.name).all())


def _render_detail(rule, test_results=None):
    runs = rule.runs.order_by(AutomationRun.created_at.desc()).limit(100).all()
    # Staged device actions without a ticket only show on the device's own
    # page otherwise, so a rule's waiting actions are listed here too.
    pending = (PendingDeviceAction.query.filter_by(rule_id=rule.id, status='pending')
               .order_by(PendingDeviceAction.created_at.desc()).limit(100).all())
    return render_template('admin_rule_detail.html', rule=rule, runs=runs, pending=pending,
                           action_labels=AUTOMATION_ACTIONS, summary=_describe_rule(rule, _Choices()),
                           test_results=test_results)


@app.route('/admin/rules/<int:rule_id>')
@require_super_admin
def admin_rule_detail(rule_id):
    return _render_detail(AutomationRule.query.get_or_404(rule_id))


@app.route('/admin/rules/<int:rule_id>/test', methods=['POST'])
@require_super_admin
def admin_rule_test(rule_id):
    """Dry run: what the rule would do against recent (or currently due)
    items, without changing anything."""
    rule = AutomationRule.query.get_or_404(rule_id)
    if rule.trigger not in TRIGGERS:
        flash('This rule\'s trigger no longer exists. Edit it to choose another.', 'error')
        return redirect(url_for('admin_rule_detail', rule_id=rule.id))
    return _render_detail(rule, test_results=test_rule(rule))


@app.route('/admin/rules/<int:rule_id>/toggle', methods=['POST'])
@require_super_admin
def admin_rule_toggle(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    rule.enabled = not rule.enabled
    _log_activity('automation_rule', f'{"Turned on" if rule.enabled else "Paused"} automation "{rule.name}".')
    db.session.commit()
    flash(f'"{rule.name}" is {"on" if rule.enabled else "paused"}.', 'success')
    target = request.form.get('next')
    return redirect(url_for('admin_rule_detail', rule_id=rule.id) if target == 'detail' else url_for('admin_rules'))


@app.route('/admin/rules/<int:rule_id>/delete', methods=['POST'])
@require_super_admin
def admin_rule_delete(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    name = rule.name
    # Staged device actions outlive the rule: a tech can still confirm or dismiss them.
    PendingDeviceAction.query.filter_by(rule_id=rule.id).update({'rule_id': None})
    db.session.delete(rule)
    _log_activity('automation_rule', f'Deleted automation "{name}".')
    db.session.commit()
    flash(f'Deleted "{name}".', 'success')
    return redirect(url_for('admin_rules'))
