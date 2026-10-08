"""What an automation rule can do.

Each action declares the subjects it works on and its settings, with the
choices to prefill (the district's own categories, staff, sites, org
units, ...). run(ctx, params, dry_run) returns (status, message) and never
raises — one failing action must not stop the rest of the rule, or the
ticket/assignment that triggered it.

Text settings are templates: {placeholder} names from
triggers.PLACEHOLDER_NAMES are filled in (unknown names are left as-is).
"""
import json
from decimal import Decimal, InvalidOperation

from foxdesk.core import EMAIL_ENABLED, db
from foxdesk.models import (
    ASSET_STATUSES, TICKET_PRIORITIES, TICKET_STATUSES,
    Asset, PendingDeviceAction, TicketCharge, TicketComment,
)
from foxdesk.automation.triggers import (
    _org_units, _plain, _repair_categories, _staff_users, _ticket_categories, placeholders,
)


class _Safe(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def render(template, ctx):
    try:
        return (template or '').format_map(_Safe(placeholders(ctx)))
    except (ValueError, IndexError):
        return template or ''


RECIPIENTS = _plain(['requester', 'holder', 'person', 'guardian', 'address'],
                    {'requester': 'The ticket requester', 'holder': 'Whoever holds the device',
                     'person': 'The person involved', 'guardian': 'The person\'s parent/guardian',
                     'address': 'A specific email address (e.g. your tech team)'})


def _recipient(ctx, who, address):
    t, person, holder = ctx.get('ticket'), ctx.get('person'), ctx.get('holder')
    if who == 'requester':
        return t.requester_email if t else None
    if who == 'holder':
        return holder.email if holder else None
    if who == 'person':
        return person.email if person else None
    if who == 'guardian':
        return person.guardian_email if person else None
    return (address or '').strip() or None


# ─── action implementations ───────────────────────────────────────────────────

def _send_email(ctx, p, dry_run):
    to = _recipient(ctx, p.get('to'), p.get('address'))
    if not to:
        return 'skipped', f'No email address for "{dict(RECIPIENTS()).get(p.get("to"), p.get("to"))}".'
    subject, body = render(p.get('subject'), ctx), render(p.get('body'), ctx)
    if dry_run:
        return 'ok', f'Would email {to}: "{subject}"'
    if not EMAIL_ENABLED:
        return 'skipped', 'Email isn\'t configured on this server.'
    from foxdesk.services.emailer import _send_email_in_background
    _send_email_in_background(to, subject, body)
    return 'ok', f'Emailed {to}: "{subject}"'


def _post_webhook(ctx, p, dry_run):
    """Teams, Slack and Google Chat incoming webhooks all accept {"text": ...}."""
    url = (p.get('url') or '').strip()
    if not url.startswith('https://'):
        return 'error', 'The webhook URL must start with https://'
    text = render(p.get('message'), ctx)
    if dry_run:
        return 'ok', f'Would post to {url.split("/")[2]}: "{text[:80]}"'
    try:
        import requests
        r = requests.post(url, data=json.dumps({'text': text}), headers={'Content-Type': 'application/json'}, timeout=10)
        if r.status_code >= 300:
            return 'error', f'Webhook answered {r.status_code}.'
        return 'ok', f'Posted to {url.split("/")[2]}.'
    except Exception as e:
        return 'error', f'Could not post: {e}'


def _ticket(ctx):
    return ctx.get('ticket')


def _set_ticket_field(field, choices_label):
    def run(ctx, p, dry_run):
        t = _ticket(ctx)
        value = p.get('value')
        if not t:
            return 'skipped', 'No ticket.'
        if field == 'assigned_to_user_id':
            value = int(value) if str(value or '').isdigit() else None
        if dry_run:
            return 'ok', f'Would set {choices_label} to {value}'
        old = getattr(t, field)
        setattr(t, field, value)
        if field == 'status' and value in ('resolved', 'closed'):
            from datetime import datetime
            t.resolved_at = t.resolved_at or datetime.utcnow()
        return 'ok', f'{choices_label}: {old} → {value}'
    return run


def _add_ticket_comment(ctx, p, dry_run):
    t = _ticket(ctx)
    if not t:
        return 'skipped', 'No ticket.'
    body = render(p.get('body'), ctx)
    email_it = p.get('email_requester') in (True, 'on', 'true', '1')
    if dry_run:
        return 'ok', f'Would add a {"reply" if email_it else "comment"}: "{body[:80]}"'
    db.session.add(TicketComment(ticket_id=t.id, body=body, author_label='Automation', emailed_to_requester=email_it))
    if email_it:
        from foxdesk.services.helpdesk import _notify_ticket_requester
        _notify_ticket_requester(t, 'ticket_reply', {'tech_name': 'FoxDesk', 'reply_body': body}, force=True)
    return 'ok', 'Comment added' + (' and emailed' if email_it else '')


def _add_ticket_charge(ctx, p, dry_run):
    t = _ticket(ctx)
    if not t:
        return 'skipped', 'No ticket.'
    try:
        amount = Decimal(str(p.get('amount') or '0')).quantize(Decimal('0.01'))
    except InvalidOperation:
        return 'error', 'The amount isn\'t a number.'
    desc = render(p.get('description'), ctx) or 'Charge'
    if dry_run:
        return 'ok', f'Would add a ${amount} charge: {desc}'
    db.session.add(TicketCharge(ticket_id=t.id, description=desc, amount=amount))
    return 'ok', f'Added ${amount} charge: {desc}'


def _create_ticket_action(ctx, p, dry_run):
    from foxdesk.services.helpdesk import _create_ticket
    category_id = int(p['category']) if str(p.get('category') or '').isdigit() else None
    if not category_id:
        return 'error', 'Pick a ticket category.'
    subject = render(p.get('subject'), ctx) or ctx['label']
    description = render(p.get('description'), ctx) or f'Created by automation: {ctx["label"]}'
    row, person = ctx.get('device'), ctx.get('person') or ctx.get('holder')
    if dry_run:
        return 'ok', f'Would create a ticket: "{subject}"'
    t = _create_ticket(category_id, subject, description, person=person, asset_tag=row.asset_tag if row else None,
                       site_id=(row.site_id if row else None) or (person.site_id if person else None),
                       priority=p.get('priority') or 'normal')
    if str(p.get('assignee') or '').isdigit():
        t.assigned_to_user_id = int(p['assignee'])
    db.session.flush()
    return 'ok', f'Created ticket #{t.id}: "{subject}"'


def _set_device_status(ctx, p, dry_run):
    row = ctx.get('device')
    if not row:
        return 'skipped', 'No device.'
    status = p.get('status')
    if status not in ASSET_STATUSES:
        return 'error', 'Pick a status.'
    if dry_run:
        return 'ok', f'Would set {row.asset_tag} to {status}'
    asset = Asset.query.filter_by(asset_tag=row.asset_tag).first()
    if not asset:
        asset = Asset(asset_tag=row.asset_tag, is_valid=True)
        db.session.add(asset)
    old, asset.status = asset.status, status
    return 'ok', f'{row.asset_tag}: {old} → {status}'


def _device_action(action_type, label):
    """Wraps the existing device actions (profile clear, disable in Google,
    send to repair, move OU). With 'confirm' on, the action is staged as a
    PendingDeviceAction for a tech to confirm, exactly like before."""
    def run(ctx, p, dry_run):
        row = ctx.get('device')
        if not row:
            return 'skipped', 'No device attached.'
        settings = {k: p[k] for k in ('repair_category', 'org_unit') if p.get(k)}
        confirm = p.get('confirm') in (True, 'on', 'true', '1')
        if dry_run:
            return 'ok', f'Would {"stage" if confirm else "run"} {label} on {row.asset_tag}'
        t = ctx.get('ticket')
        pending = PendingDeviceAction(ticket_id=t.id if t else None, asset_tag=row.asset_tag, action_type=action_type,
                                      status='pending', rule_id=ctx['rule'].id, params=settings)
        db.session.add(pending)
        db.session.flush()
        if confirm:
            return 'ok', f'{label} staged on {row.asset_tag} — waiting for a tech to confirm.'
        from foxdesk.services.helpdesk import _run_pending_device_action
        status, message = _run_pending_device_action(pending, resolved_by='Automation')
        return ('ok' if status == 'ok' else 'error'), message
    return run


def _notify_guardian(ctx, p, dry_run):
    inc = ctx.get('incident')
    if not inc:
        return 'skipped', 'No damage report.'
    if dry_run:
        person = ctx.get('person')
        return 'ok', f'Would email the damage notice to {getattr(person, "guardian_email", None) or "(no guardian email)"}'
    from foxdesk.services.incidents import _send_damage_notice
    ok, message = _send_damage_notice(inc)
    return ('ok' if ok else 'skipped'), message


def _set_incident_fee(ctx, p, dry_run):
    inc = ctx.get('incident')
    if not inc:
        return 'skipped', 'No damage report.'
    try:
        amount = Decimal(str(p.get('amount') or '0')).quantize(Decimal('0.01'))
    except InvalidOperation:
        return 'error', 'The amount isn\'t a number.'
    if dry_run:
        return 'ok', f'Would set the fee to ${amount}'
    inc.fee_amount = amount
    inc.fee_charged = amount > 0
    return 'ok', f'Fee set to ${amount}'


# ─── catalog ──────────────────────────────────────────────────────────────────
# params: (key, label, type, choices, default)  types: text | textarea | choice | bool | number

_TEMPLATE_HINT = 'You can use placeholders like {asset_tag}, {person_name}, {ticket_id}.'

ACTIONS = {
    'send_email': dict(label='Send an email', subjects='*', run=_send_email, params=[
        ('to', 'To', 'choice', RECIPIENTS, 'holder'),
        ('address', 'Address (when "A specific email address")', 'text', None, ''),
        ('subject', 'Subject', 'text', None, ''),
        ('body', 'Message', 'textarea', None, ''),
    ]),
    'post_webhook': dict(label='Post to Teams / Slack / Google Chat', subjects='*', run=_post_webhook, params=[
        ('url', 'Incoming webhook URL', 'text', None, ''),
        ('message', 'Message', 'textarea', None, ''),
    ]),
    'set_priority': dict(label='Set ticket priority', subjects=('ticket',), run=_set_ticket_field('priority', 'Priority'),
                         params=[('value', 'Priority', 'choice', _plain(TICKET_PRIORITIES), 'high')]),
    'set_status': dict(label='Set ticket status', subjects=('ticket',), run=_set_ticket_field('status', 'Status'),
                       params=[('value', 'Status', 'choice', _plain(TICKET_STATUSES), 'in_progress')]),
    'set_category': dict(label='Set ticket category', subjects=('ticket',), run=_set_ticket_field('category_id', 'Category'),
                         params=[('value', 'Category', 'choice', _ticket_categories, None)]),
    'assign_ticket': dict(label='Assign the ticket to a tech', subjects=('ticket',),
                          run=_set_ticket_field('assigned_to_user_id', 'Assignee'),
                          params=[('value', 'Tech', 'choice', _staff_users, None)]),
    'add_comment': dict(label='Add a comment to the ticket', subjects=('ticket',), run=_add_ticket_comment, params=[
        ('body', 'Comment', 'textarea', None, ''),
        ('email_requester', 'Also email it to the requester', 'bool', None, False),
    ]),
    'add_charge': dict(label='Add a charge to the ticket', subjects=('ticket',), run=_add_ticket_charge, params=[
        ('description', 'Charge for', 'text', None, ''),
        ('amount', 'Amount ($)', 'number', None, ''),
    ]),
    'create_ticket': dict(label='Create a ticket', subjects='*', run=_create_ticket_action, params=[
        ('category', 'Category', 'choice', _ticket_categories, None),
        ('subject', 'Subject', 'text', None, ''),
        ('description', 'Description', 'textarea', None, ''),
        ('priority', 'Priority', 'choice', _plain(TICKET_PRIORITIES), 'normal'),
        ('assignee', 'Assign to', 'choice', _staff_users, None),
    ]),
    'set_device_status': dict(label='Set the device status', subjects=('ticket', 'device', 'incident', 'repair', 'loaner', 'signin'),
                              run=_set_device_status, params=[('status', 'Status', 'choice', _plain(ASSET_STATUSES), 'repair')]),
    'send_to_repair': dict(label='Send the device to repair', subjects=('ticket', 'device', 'incident', 'signin'),
                           run=_device_action('send_to_repair', 'Send to Repair'), feature='repairs', params=[
        ('repair_category', 'Repair category', 'choice', _repair_categories, None),
        ('confirm', 'Wait for a tech to confirm', 'bool', None, True),
    ]),
    'profile_clear': dict(label='Clear user profiles on the Chromebook', subjects=('ticket', 'device', 'signin'),
                          run=_device_action('profile_clear', 'Profile Clear'), feature='google', params=[
        ('confirm', 'Wait for a tech to confirm', 'bool', None, True),
    ]),
    'disable_google': dict(label='Disable the Chromebook in Google', subjects=('ticket', 'device', 'signin', 'loaner'),
                           run=_device_action('disable_google', 'Disable in Google'), feature='google', params=[
        ('confirm', 'Wait for a tech to confirm', 'bool', None, True),
    ]),
    'move_device': dict(label='Move the Chromebook to an org unit', subjects=('ticket', 'device', 'signin'),
                        run=_device_action('move_device', 'Move to Org Unit'), feature='google', params=[
        ('org_unit', 'Org unit', 'choice', _org_units, None),
        ('confirm', 'Wait for a tech to confirm', 'bool', None, True),
    ]),
    'notify_guardian': dict(label='Email the parent/guardian damage notice', subjects=('incident',),
                            run=_notify_guardian, feature='guardian_notices', params=[]),
    'set_incident_fee': dict(label='Set the damage fee', subjects=('incident',), run=_set_incident_fee,
                             feature='incidents', params=[('amount', 'Fee ($)', 'number', None, '')]),
}


def actions_for(subject_type):
    from foxdesk.services.features import feature_enabled
    return {k: a for k, a in ACTIONS.items()
            if (a['subjects'] == '*' or subject_type in a['subjects'])
            and (not a.get('feature') or feature_enabled(a['feature']))}
