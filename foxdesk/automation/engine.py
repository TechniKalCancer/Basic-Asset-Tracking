"""Runs automation rules.

emit(trigger, subject) is called by the app right after something happens
(and has been committed): a ticket created, a device assigned, damage
reported... Rules for that trigger whose conditions match run their actions
in order. Nothing here ever raises into the caller — an automation failing
must never undo or block the thing that triggered it; failures are recorded
on the rule's run history instead.

Scheduled triggers (overdue repairs/loaners, expiring warranties, sign-in
flags) are checked by run_scheduled() from the background loop, and fire
once per subject per rule — enforced by a unique (rule, dedupe_key) run
row, so several gunicorn workers checking at once can't double-fire.
"""
from datetime import date, datetime, timedelta

from flask import g, has_request_context
from sqlalchemy.exc import IntegrityError

from foxdesk.core import db, logger
from foxdesk.models import (
    ActivityLog, AssetRegistry, AssignmentHistory, AutomationRule, AutomationRun, Incident, LoanerCheckout,
    Person, Repair, Ticket,
)
from foxdesk.automation.actions import ACTIONS, actions_for
from foxdesk.automation.triggers import FIELDS, TRIGGERS, build_context, current_period

MAX_DEPTH = 3  # an automation's own changes can't chain more than this deep


# ─── conditions ───────────────────────────────────────────────────────────────

def _as_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_condition(cond, facts):
    """(matched, explanation) for one {"field", "op", "value"} condition."""
    field, op, expected = cond.get('field'), cond.get('op'), cond.get('value')
    label, ftype, _ = FIELDS.get(field, (field, 'text', None))
    actual = facts.get(field)
    if op == 'empty':
        ok = actual in (None, '', [])
    elif op == 'not_empty':
        ok = actual not in (None, '', [])
    elif ftype == 'bool' or op in ('true', 'false'):
        ok = bool(actual) == (op == 'true')
    elif ftype == 'number':
        a, e = _as_number(actual), _as_number(expected)
        if a is None or e is None:
            ok = False
        else:
            ok = {'eq': a == e, 'ne': a != e, 'gt': a > e, 'gte': a >= e, 'lt': a < e, 'lte': a <= e}.get(op, False)
    elif op == 'in':
        wanted = expected if isinstance(expected, list) else [x.strip() for x in str(expected or '').split(',')]
        ok = str(actual) in {str(w) for w in wanted}
    else:
        a, e = str(actual if actual is not None else '').lower(), str(expected if expected is not None else '').lower()
        ok = {'eq': a == e, 'ne': a != e, 'contains': e in a, 'not_contains': e not in a,
              'starts': a.startswith(e)}.get(op, False)
    return ok, f'{label} {op} {expected!r} (was {actual!r})'


def evaluate(rule, facts):
    conds = rule.conditions or []
    if not conds:
        return True, ['No conditions — every time.']
    results = [check_condition(c, facts) for c in conds]
    ok = all(r[0] for r in results) if (rule.match or 'all') == 'all' else any(r[0] for r in results)
    return ok, [('✓ ' if r[0] else '✗ ') + r[1] for r in results]


def _subject_site(facts):
    return facts.get('ticket.site') or facts.get('device.site') or facts.get('person.site') or facts.get('part.site')


# ─── running ──────────────────────────────────────────────────────────────────

def run_rule(rule, ctx, dry_run=False, dedupe_key=None):
    """Run one rule against one subject. Returns the run dict, or None when
    the rule doesn't apply (site, conditions, or already ran for this
    scheduled subject). Commits unless dry_run."""
    facts = ctx['facts']
    # A time-based rule's school limits what its report covers, not whether it runs.
    if rule.site_id and ctx['subject_type'] != 'period' and _subject_site(facts) != rule.site_id:
        return None
    matched, reasons = evaluate(rule, facts)
    if not matched:
        return {'matched': False, 'reasons': reasons, 'results': []} if dry_run else None

    run = None
    if not dry_run:
        run = AutomationRun(rule_id=rule.id, trigger=ctx['trigger'], subject_label=ctx['label'][:255],
                            dedupe_key=dedupe_key, status='ran')
        if dedupe_key:
            try:
                with db.session.begin_nested():
                    db.session.add(run)
            except IntegrityError:
                return None  # already ran for this subject (or another worker just claimed it)
        else:
            db.session.add(run)

    ctx = dict(ctx, rule=rule)
    available = actions_for(ctx['subject_type'])
    results = []
    for step in rule.actions or []:
        kind = step.get('type')
        spec = ACTIONS.get(kind)
        if spec is None or kind not in available:
            results.append({'action': kind, 'status': 'skipped', 'message': 'This action isn\'t available here.'})
            continue
        try:
            status, message = spec['run'](ctx, step.get('params') or {}, dry_run)
        except Exception as e:  # an action bug must not stop the others
            logger.exception('Automation %s action %s failed', rule.id, kind)
            status, message = 'error', f'Unexpected error: {e}'
        results.append({'action': spec['label'], 'status': status, 'message': message})

    if dry_run:
        return {'matched': True, 'reasons': reasons, 'results': results}

    run.results = results
    run.status = 'error' if any(r['status'] == 'error' for r in results) else 'ran'
    rule.run_count = (rule.run_count or 0) + 1
    rule.last_run_at = datetime.utcnow()
    db.session.add(ActivityLog(actor_type='system', actor_label=f'Automation: {rule.name}'[:160],
                               action='automation_run', site_id=_subject_site(facts),
                               ticket_id=ctx['ticket'].id if ctx.get('ticket') else None,
                               summary=f'{ctx["label"]}: ' + '; '.join(r['message'] for r in results)))
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error('Automation %s could not save its run: %s', rule.id, e)
        return None
    return {'matched': True, 'reasons': reasons, 'results': results}


def emit(trigger_key, subject, **extra):
    """Something happened — run the enabled rules for it. Never raises."""
    try:
        from foxdesk.services.features import feature_enabled
        spec = TRIGGERS[trigger_key]
        if not feature_enabled('automations') or (spec.get('feature') and not feature_enabled(spec['feature'])):
            return
        depth = getattr(g, '_automation_depth', 0) if has_request_context() else 0
        if depth >= MAX_DEPTH:
            return
        rules = AutomationRule.query.filter_by(trigger=trigger_key, enabled=True).order_by(AutomationRule.id).all()
        if not rules:
            return
        if has_request_context():
            g._automation_depth = depth + 1
        try:
            ctx = build_context(trigger_key, subject, **extra)
            for rule in rules:
                run_rule(rule, ctx)
        finally:
            if has_request_context():
                g._automation_depth = depth
    except Exception as e:
        db.session.rollback()
        logger.exception('Automation trigger %s failed: %s', trigger_key, e)


# ─── candidates: recent subjects (for tests) and scheduled subjects ──────────

def _recent_subjects(trigger_key, limit=25):
    kind = TRIGGERS[trigger_key]['subject']
    if kind == 'ticket':
        return Ticket.query.order_by(Ticket.created_at.desc()).limit(limit).all()
    if kind == 'incident':
        return Incident.query.order_by(Incident.created_at.desc()).limit(limit).all()
    if kind == 'repair':
        return Repair.query.order_by(Repair.sent_at.desc()).limit(limit).all()
    if kind == 'loaner':
        return LoanerCheckout.query.order_by(LoanerCheckout.checked_out_at.desc()).limit(limit).all()
    if kind == 'person':
        return Person.query.filter_by(is_active=False).order_by(Person.id.desc()).limit(limit).all()
    if kind == 'device':
        tags = [h.asset_tag for h in AssignmentHistory.query.order_by(AssignmentHistory.assigned_at.desc()).limit(limit)]
        rows = AssetRegistry.query.filter(AssetRegistry.asset_tag.in_(tags)).all() if tags else []
        return rows or AssetRegistry.query.order_by(AssetRegistry.id.desc()).limit(limit).all()
    return []


def scheduled_subjects(trigger_key):
    """(subject, dedupe_key) pairs a scheduled trigger should consider now."""
    today = date.today()
    if trigger_key == 'repair.overdue':
        return [(r, f'repair:{r.id}') for r in Repair.query.filter(Repair.returned_at.is_(None))]
    if trigger_key == 'loaner.overdue':
        return [(l, f'loaner:{l.id}') for l in LoanerCheckout.query.filter(
            LoanerCheckout.checked_in_at.is_(None), LoanerCheckout.due_date.isnot(None), LoanerCheckout.due_date < today)]
    if trigger_key == 'device.warranty_expiring':
        horizon = today + timedelta(days=365)
        return [(r, f'device:{r.id}:{r.warranty_expiration}') for r in AssetRegistry.query.filter(
            AssetRegistry.warranty_expiration.isnot(None), AssetRegistry.warranty_expiration >= today,
            AssetRegistry.warranty_expiration <= horizon)]
    if trigger_key.startswith('schedule.'):
        period = current_period(trigger_key.split('.', 1)[1])
        return [(period, period['key'])]
    if trigger_key == 'part.low_stock':
        from foxdesk.models import Part, PartMovement
        low = Part.query.filter(Part.is_active.is_(True), Part.reorder_level > 0,
                                Part.quantity_on_hand <= Part.reorder_level).all()
        # Keyed on the last restock, so a part fires again if it runs low again after being refilled.
        restocked = dict(db.session.query(PartMovement.part_id, db.func.max(PartMovement.id))
                         .filter(PartMovement.change > 0).group_by(PartMovement.part_id).all())
        return [(p, f'part:{p.id}:{restocked.get(p.id, 0)}') for p in low]
    if trigger_key == 'signin.flagged':
        from foxdesk.services.reports import _signin_mismatches
        return [(m, f"signin:{m['asset_tag']}:{m['signin_email']}") for m in _signin_mismatches(None, 7)]
    return []


def run_scheduled():
    """Check every scheduled trigger that has an enabled rule. Safe to call
    from several processes at once (see module docstring)."""
    from foxdesk.services.features import feature_enabled
    if not feature_enabled('automations'):
        return 0
    fired = 0
    for trigger_key, spec in TRIGGERS.items():
        if not spec.get('scheduled') or (spec.get('feature') and not feature_enabled(spec['feature'])):
            continue
        rules = AutomationRule.query.filter_by(trigger=trigger_key, enabled=True).all()
        if not rules:
            continue
        for subject, key in scheduled_subjects(trigger_key):
            ctx = build_context(trigger_key, subject)
            for rule in rules:
                if run_rule(rule, ctx, dedupe_key=key):
                    fired += 1
    return fired


def test_rule(rule, limit=25):
    """Dry run against recent (or currently due) subjects: what would match
    and what each action would do. Changes nothing."""
    spec = TRIGGERS[rule.trigger]
    if spec.get('scheduled'):
        subjects = [s for s, _ in scheduled_subjects(rule.trigger)][:200]
    else:
        subjects = _recent_subjects(rule.trigger, limit)
    out = []
    for subject in subjects:
        ctx = build_context(rule.trigger, subject)
        result = run_rule(rule, ctx, dry_run=True)
        if result is not None:
            out.append(dict(label=ctx['label'], **result))
    return out
