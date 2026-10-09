"""What can start an automation rule, and what a rule can test.

A trigger names a moment ("a ticket was created", "a repair has been out
too long") and the subject it's about. Each trigger offers a list of fields
a rule's conditions can test; every field knows its type and, where it
makes sense, the choices to offer (the district's own ticket categories,
sites, device models, staff ...), so the rule builder can prefill
dropdowns instead of asking people to type IDs.

Facts are flat {field_key: value} dicts built from the subject when a
trigger fires; conditions compare against them and email/webhook templates
read the friendlier `placeholders()` built alongside.
"""
from datetime import date, datetime, time, timedelta, timezone

from foxdesk.core import APP_TIMEZONE

from foxdesk.models import (
    PART_CATEGORIES,
    ASSET_STATUSES, DEVICE_TYPES, REPAIR_OUTCOMES, TICKET_PRIORITIES, TICKET_STATUSES,
    Asset, AssetRegistry, DeviceModel, GoogleOrgUnit, Incident, Person, RepairCategory, Site, TicketCategory, User,
)


# ─── choices (prefilled dropdowns) ────────────────────────────────────────────

def _sites():
    return [(s.id, s.name) for s in Site.query.order_by(Site.name)]


def _ticket_categories():
    return [(c.id, c.name) for c in TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name)]


def _repair_categories():
    return [(c.id, c.name) for c in RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name)]


def _device_models():
    return [(m.id, m.full_name) for m in DeviceModel.query.order_by(DeviceModel.manufacturer, DeviceModel.model_name)]


def _staff_users():
    return [(u.id, u.username) for u in User.query.filter_by(is_active=True).order_by(User.username)]


def _org_units():
    return [(o.org_unit_path, o.org_unit_path) for o in GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path)]


def _plain(values, labels=None):
    return lambda: [(v, (labels or {}).get(v, str(v).replace('_', ' ').capitalize())) for v in values]


ROLES = _plain(['student', 'staff'])
SIGNIN_CATEGORY_CHOICES = _plain(['wrong_student', 'swapped', 'inactive_person', 'lost_in_use', 'unassigned_in_use',
                                  'same_name', 'unknown_account', 'previous_holder', 'staff_signin'],
                                 {'wrong_student': "Another student's device", 'swapped': 'Swapped devices',
                                  'inactive_person': 'Withdrawn/graduated account', 'lost_in_use': 'Lost/retired device in use',
                                  'unassigned_in_use': 'Unassigned device in use', 'same_name': 'Same name, different account',
                                  'unknown_account': 'Account not in People', 'previous_holder': 'Previous holder',
                                  'staff_signin': 'Staff sign-in'})

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
WEEKDAY_CHOICES = _plain(WEEKDAYS)

# ─── fields ───────────────────────────────────────────────────────────────────
# key: (label, type, choices)   types: text | choice | number | bool

FIELDS = {
    'ticket.category':      ('Ticket category', 'choice', _ticket_categories),
    'ticket.priority':      ('Ticket priority', 'choice', _plain(TICKET_PRIORITIES)),
    'ticket.status':        ('Ticket status', 'choice', _plain(TICKET_STATUSES)),
    'ticket.old_status':    ('Previous ticket status', 'choice', _plain(TICKET_STATUSES)),
    'ticket.site':          ('Ticket school/site', 'choice', _sites),
    'ticket.subject':       ('Ticket subject', 'text', None),
    'ticket.description':   ('Ticket description', 'text', None),
    'ticket.has_device':    ('Ticket has a device attached', 'bool', None),
    'ticket.assignee':      ('Ticket assigned to', 'choice', _staff_users),
    'ticket.requester_role': ('Requester role', 'choice', ROLES),

    'device.type':          ('Device type', 'choice', _plain(DEVICE_TYPES)),
    'device.model':         ('Device model', 'choice', _device_models),
    'device.site':          ('Device school/site', 'choice', _sites),
    'device.status':        ('Device status', 'choice', _plain(ASSET_STATUSES)),
    'device.is_loaner':     ('Device is a loaner', 'bool', None),
    'device.asset_tag':     ('Asset tag', 'text', None),
    'device.org_unit':      ('Device Google org unit', 'text', None),
    'device.warranty_days_left': ('Days of warranty left', 'number', None),

    'holder.role':          ('Device holder role', 'choice', ROLES),
    'holder.grad_year':     ('Device holder graduation year', 'number', None),
    'holder.has_protection_plan': ('Device holder has the protection plan', 'bool', None),

    'person.role':          ('Person role', 'choice', ROLES),
    'person.site':          ('Person school/site', 'choice', _sites),
    'person.grad_year':     ('Graduation year', 'number', None),
    'person.device_count':  ('Devices the person holds', 'number', None),
    'person.incident_count': ('Damage reports for this person (all time)', 'number', None),
    'person.has_protection_plan': ('Person has the protection plan', 'bool', None),
    'person.has_guardian_email': ('Person has a parent/guardian email', 'bool', None),

    'incident.category':    ('Damage type', 'choice', _repair_categories),
    'incident.fee_amount':  ('Fee amount', 'number', None),
    'incident.fee_charged': ('Fee charged', 'bool', None),
    'incident.description': ('Damage description', 'text', None),

    'repair.category':      ('Repair category', 'choice', _repair_categories),
    'repair.days_out':      ('Days out for repair', 'number', None),
    'repair.outcome':       ('Repair outcome', 'choice', _plain(list(REPAIR_OUTCOMES), REPAIR_OUTCOMES)),

    'loaner.days_out':      ('Days the loaner has been out', 'number', None),
    'loaner.days_overdue':  ('Days the loaner is overdue', 'number', None),

    'signin.category':      ('Sign-in flag', 'choice', SIGNIN_CATEGORY_CHOICES),
    'signin.severity':      ('Flag severity', 'choice', _plain(['high', 'medium', 'low'])),
    'signin.foreign_count': ('Devices that aren\'t theirs the account is on', 'number', None),

    'schedule.weekday':     ('Day of the week', 'choice', WEEKDAY_CHOICES),
    'schedule.hour':        ('Hour of the day (0-23, district time)', 'number', None),
    'schedule.day':         ('Day of the month', 'number', None),

    'part.name':            ('Part name', 'text', None),
    'part.category':        ('Part category', 'choice', _plain(PART_CATEGORIES)),
    'part.on_hand':         ('Parts on hand', 'number', None),
    'part.reorder_level':   ('Reorder level', 'number', None),
    'part.site':            ('Part kept at', 'choice', _sites),
}

OPERATORS = {
    'text':   [('contains', 'contains'), ('not_contains', 'does not contain'), ('eq', 'is'), ('ne', 'is not'),
               ('starts', 'starts with'), ('empty', 'is empty'), ('not_empty', 'is not empty')],
    'choice': [('eq', 'is'), ('ne', 'is not'), ('in', 'is any of'), ('empty', 'is empty'), ('not_empty', 'is not empty')],
    'number': [('eq', '='), ('ne', '≠'), ('gt', '>'), ('gte', '≥'), ('lt', '<'), ('lte', '≤'), ('empty', 'is empty')],
    'bool':   [('true', 'is yes'), ('false', 'is no')],
}

DEVICE_FIELDS = ['device.type', 'device.model', 'device.site', 'device.status', 'device.is_loaner', 'device.asset_tag',
                 'device.org_unit', 'device.warranty_days_left', 'holder.role', 'holder.grad_year',
                 'holder.has_protection_plan']
PERSON_FIELDS = ['person.role', 'person.site', 'person.grad_year', 'person.device_count', 'person.incident_count',
                 'person.has_protection_plan', 'person.has_guardian_email']
TICKET_FIELDS = ['ticket.category', 'ticket.priority', 'ticket.status', 'ticket.site', 'ticket.subject',
                 'ticket.description', 'ticket.has_device', 'ticket.assignee', 'ticket.requester_role']

# ─── triggers ─────────────────────────────────────────────────────────────────
# subject: what the rule acts on. scheduled: checked by the background loop
# (once per subject per rule) instead of fired by something happening.

TRIGGERS = {
    'ticket.created':        dict(label='A ticket is created', group='Tickets', subject='ticket', feature='tickets',
                                  fields=TICKET_FIELDS + DEVICE_FIELDS + PERSON_FIELDS),
    'ticket.status_changed': dict(label='A ticket\'s status changes', group='Tickets', subject='ticket', feature='tickets',
                                  fields=TICKET_FIELDS + ['ticket.old_status'] + DEVICE_FIELDS),
    'incident.created':      dict(label='Damage is reported', group='Devices', subject='incident', feature='incidents',
                                  fields=['incident.category', 'incident.fee_amount', 'incident.fee_charged',
                                          'incident.description'] + DEVICE_FIELDS + PERSON_FIELDS),
    'device.assigned':       dict(label='A device is assigned to someone', group='Devices', subject='device',
                                  fields=DEVICE_FIELDS + PERSON_FIELDS),
    'device.unassigned':     dict(label='A device is unassigned', group='Devices', subject='device',
                                  fields=DEVICE_FIELDS + PERSON_FIELDS),
    'device.status_changed': dict(label='A device\'s status changes', group='Devices', subject='device',
                                  fields=DEVICE_FIELDS),
    'repair.sent':           dict(label='A device is sent for repair', group='Repairs', subject='repair', feature='repairs',
                                  fields=['repair.category'] + DEVICE_FIELDS),
    'repair.returned':       dict(label='A repaired device comes back', group='Repairs', subject='repair', feature='repairs',
                                  fields=['repair.category', 'repair.outcome', 'repair.days_out'] + DEVICE_FIELDS),
    'loaner.checked_out':    dict(label='A loaner is checked out', group='Loaners', subject='loaner', feature='loaners',
                                  fields=DEVICE_FIELDS + PERSON_FIELDS),
    'loaner.checked_in':     dict(label='A loaner is returned', group='Loaners', subject='loaner', feature='loaners',
                                  fields=['loaner.days_out'] + DEVICE_FIELDS + PERSON_FIELDS),
    'person.deactivated':    dict(label='A person leaves (graduated/withdrawn)', group='People', subject='person',
                                  fields=PERSON_FIELDS),
    # scheduled — checked by the background loop, once per subject per rule
    'repair.overdue':        dict(label='A repair has been out a while (checked daily)', group='Repairs', subject='repair',
                                  feature='repairs', scheduled=True,
                                  fields=['repair.category', 'repair.days_out'] + DEVICE_FIELDS),
    'loaner.overdue':        dict(label='A loaner is overdue (checked daily)', group='Loaners', subject='loaner',
                                  feature='loaners', scheduled=True,
                                  fields=['loaner.days_overdue', 'loaner.days_out'] + DEVICE_FIELDS + PERSON_FIELDS),
    'device.warranty_expiring': dict(label='A warranty is running out (checked daily)', group='Devices', subject='device',
                                     scheduled=True, fields=DEVICE_FIELDS),
    'signin.flagged':        dict(label='The Google sign-in check flags a device (checked daily)', group='Devices',
                                  subject='signin', feature='signin_check', scheduled=True,
                                  fields=['signin.category', 'signin.severity', 'signin.foreign_count']
                                  + DEVICE_FIELDS + PERSON_FIELDS),
    'part.low_stock':        dict(label='A part runs low (checked every 15 min)', group='Repairs', subject='part',
                                  feature='parts', scheduled=True,
                                  fields=['part.name', 'part.category', 'part.on_hand', 'part.reorder_level', 'part.site']),
    # time-based: once per day/week/month, at the first check after the conditions match
    'schedule.daily':        dict(label='Every day', group='Schedule', subject='period', scheduled=True,
                                  fields=['schedule.weekday', 'schedule.hour'],
                                  hint='Runs once a day, at the first check (every 15 minutes) where the conditions '
                                       'match, e.g. hour ≥ 7 for 7 AM. Reports cover yesterday.'),
    'schedule.weekly':       dict(label='Every week', group='Schedule', subject='period', scheduled=True,
                                  fields=['schedule.weekday', 'schedule.hour'],
                                  hint='Runs once a week, at the first check where the conditions match, e.g. day is '
                                       'Monday and hour ≥ 7. Reports cover last Monday to Sunday.'),
    'schedule.monthly':      dict(label='Every month', group='Schedule', subject='period', scheduled=True,
                                  fields=['schedule.day', 'schedule.hour'],
                                  hint='Runs once a month, at the first check where the conditions match, e.g. hour ≥ 7 '
                                       'on the 1st. Reports cover last month.'),
}
TRIGGER_GROUPS = ['Tickets', 'Devices', 'Repairs', 'Loaners', 'People', 'Schedule']


# ─── periods (time-based triggers) ────────────────────────────────────────────

def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception:
        return ZoneInfo('UTC')


def local_now():
    return datetime.now(_tz())


def current_period(kind, now=None):
    """What a daily/weekly/monthly run right now is about. The report sent
    this morning covers what already happened: yesterday, last Monday to
    Sunday, or last month. key is the dedupe key — one run per day / week /
    month — and start/end are naive UTC for querying."""
    now = now or local_now()
    tz = now.tzinfo or _tz()
    today = now.date()
    if kind == 'daily':
        start, end, key = today - timedelta(days=1), today, f'day:{today}'
        label = f'{start:%A, %B} {start.day}'
    elif kind == 'weekly':
        monday = today - timedelta(days=today.weekday())
        start, end, key = monday - timedelta(days=7), monday, f'week:{monday}'
        label = f'the week of {start:%B} {start.day}'
    else:
        first = today.replace(day=1)
        start, end, key = (first - timedelta(days=1)).replace(day=1), first, f'month:{first:%Y-%m}'
        label = f'{start:%B %Y}'

    def utc(d):
        return datetime.combine(d, time(0), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return dict(kind=kind, key=key, start=utc(start), end=utc(end), label=label, now=now)


# ─── facts ────────────────────────────────────────────────────────────────────

def _device_facts(row, asset):
    facts = {}
    if row is None:
        return facts
    holder = asset.assigned_to if asset else None
    warranty_left = (row.warranty_expiration - date.today()).days if row.warranty_expiration else None
    facts.update({
        'device.type': row.device_type, 'device.model': row.device_model_id, 'device.site': row.site_id,
        'device.status': asset.status if asset else 'available', 'device.is_loaner': bool(row.is_loaner),
        'device.asset_tag': row.asset_tag, 'device.org_unit': asset.google_org_unit if asset else None,
        'device.warranty_days_left': warranty_left,
        'holder.role': holder.role if holder else None, 'holder.grad_year': holder.grad_year if holder else None,
        'holder.has_protection_plan': bool(holder and holder.insurance_opted_in),
    })
    return facts


def _person_facts(person):
    if person is None:
        return {}
    device_count = Asset.query.filter_by(assigned_to_id=person.id).count()
    incident_count = Incident.query.filter_by(person_id=person.id).count()
    return {
        'person.role': person.role, 'person.site': person.site_id, 'person.grad_year': person.grad_year,
        'person.device_count': device_count, 'person.incident_count': incident_count,
        'person.has_protection_plan': bool(person.insurance_opted_in),
        'person.has_guardian_email': bool(person.guardian_email),
    }


def build_context(trigger_key, subject, **extra):
    """Everything a rule can test or use: the subject's related records
    ('ticket', 'device' registry row, 'asset', 'person', 'holder', 'incident',
    'repair', 'loaner', 'signin') plus the flat facts dict."""
    ctx = dict(trigger=trigger_key, subject_type=TRIGGERS[trigger_key]['subject'], extra=extra)
    kind = ctx['subject_type']
    row = asset = person = None
    if kind == 'ticket':
        ctx['ticket'] = subject
        row = AssetRegistry.query.filter_by(asset_tag=subject.asset_tag).first() if subject.asset_tag else None
        person = subject.requester
    elif kind == 'incident':
        ctx['incident'] = subject
        row = AssetRegistry.query.filter_by(asset_tag=subject.asset_tag).first()
        person = Person.query.get(subject.person_id) if subject.person_id else None
    elif kind == 'repair':
        ctx['repair'] = subject
        row = AssetRegistry.query.filter_by(asset_tag=subject.asset_tag).first()
    elif kind == 'loaner':
        ctx['loaner'] = subject
        row = AssetRegistry.query.filter_by(asset_tag=subject.asset_tag).first()
        person = Person.query.get(subject.person_id) if subject.person_id else None
    elif kind == 'device':
        row = subject
        person = extra.get('person')
    elif kind == 'person':
        person = subject
    elif kind == 'signin':
        ctx['signin'] = subject
        row = subject['registry_row']
        person = subject.get('signer')
    elif kind == 'period':
        ctx['period'] = subject
    elif kind == 'part':
        ctx['part'] = subject
    if row is not None:
        asset = Asset.query.filter_by(asset_tag=row.asset_tag).first()
    if person is None and asset is not None and kind in ('device', 'repair'):
        person = asset.assigned_to
    ctx.update(device=row, asset=asset, person=person, holder=asset.assigned_to if asset else None)

    facts = {}
    facts.update(_device_facts(row, asset))
    facts.update(_person_facts(person))
    if 'device_count' in extra:  # e.g. graduation: devices already unassigned but still in hand
        facts['person.device_count'] = extra['device_count']
    now = datetime.utcnow()
    if kind == 'ticket':
        t = subject
        facts.update({
            'ticket.category': t.category_id, 'ticket.priority': t.priority, 'ticket.status': t.status,
            'ticket.old_status': extra.get('old_status'), 'ticket.site': t.site_id, 'ticket.subject': t.subject,
            'ticket.description': t.description, 'ticket.has_device': bool(t.asset_tag),
            'ticket.assignee': t.assigned_to_user_id,
            'ticket.requester_role': t.requester.role if t.requester else None,
        })
    elif kind == 'incident':
        i = subject
        facts.update({'incident.category': i.repair_category_id,
                      'incident.fee_amount': float(i.fee_amount) if i.fee_amount is not None else None,
                      'incident.fee_charged': bool(i.fee_charged), 'incident.description': i.description})
    elif kind == 'repair':
        r = subject
        facts.update({'repair.category': r.repair_category_id, 'repair.outcome': r.outcome,
                      'repair.days_out': ((r.returned_at or now) - r.sent_at).days})
    elif kind == 'loaner':
        l = subject
        facts.update({'loaner.days_out': ((l.checked_in_at or now) - l.checked_out_at).days,
                      'loaner.days_overdue': (date.today() - l.due_date).days if l.due_date else None})
    elif kind == 'signin':
        facts.update({'signin.category': subject['category'], 'signin.severity': subject['severity'],
                      'signin.foreign_count': subject.get('foreign_count', 0)})
    elif kind == 'period':
        when = subject['now']
        facts.update({'schedule.weekday': WEEKDAYS[when.weekday()], 'schedule.hour': when.hour, 'schedule.day': when.day})
    elif kind == 'part':
        facts.update({'part.name': subject.name, 'part.category': subject.category, 'part.on_hand': subject.quantity_on_hand,
                      'part.reorder_level': subject.reorder_level, 'part.site': subject.site_id})
    ctx['facts'] = facts
    ctx['label'] = subject_label(ctx)
    return ctx


def subject_label(ctx):
    kind = ctx['subject_type']
    if kind == 'ticket':
        return f"Ticket #{ctx['ticket'].id}: {ctx['ticket'].subject}"
    if kind == 'incident':
        return f"Damage on {ctx['incident'].asset_tag}: {ctx['incident'].description}"
    if kind == 'repair':
        return f"Repair of {ctx['repair'].asset_tag}"
    if kind == 'loaner':
        return f"Loaner {ctx['loaner'].asset_tag} ({ctx['loaner'].person_name})"
    if kind == 'device':
        return f"Device {ctx['device'].asset_tag}"
    if kind == 'person':
        return ctx['person'].full_name
    if kind == 'signin':
        return f"{ctx['signin']['signin_email']} on {ctx['signin']['asset_tag']}"
    if kind == 'period':
        return f"Report for {ctx['period']['label']}"
    if kind == 'part':
        return f"{ctx['part'].name} ({ctx['part'].quantity_on_hand} on hand)"
    return kind


def placeholders(ctx):
    """{name: text} for email/webhook/ticket templates. Every name always
    exists (blank when it doesn't apply) so a template never breaks."""
    t, inc = ctx.get('ticket'), ctx.get('incident')
    row, person, holder = ctx.get('device'), ctx.get('person'), ctx.get('holder')
    f = ctx['facts']
    return {
        'ticket_id': str(t.id) if t else '', 'ticket_subject': t.subject if t else '',
        'ticket_priority': t.priority if t else '', 'ticket_status': (t.status or '').replace('_', ' ') if t else '',
        'ticket_category': t.category.name if t and t.category else '',
        'requester_name': (t.requester_name or '') if t else '',
        'asset_tag': row.asset_tag if row else '', 'serial_number': (row.serial_number or '') if row else '',
        'device_model': (row.device_model.full_name if row and row.device_model else (row.description or '') if row else ''),
        'device_site': row.site.name if row and row.site else '',
        'holder_name': holder.full_name if holder else '', 'holder_email': holder.email if holder else '',
        'person_name': person.full_name if person else '', 'person_email': person.email if person else '',
        'person_first_name': person.first_name if person else '',
        'guardian_name': (person.guardian_name or '') if person else '',
        'incident_description': inc.description if inc else '',
        'fee_amount': f'{inc.fee_amount:.2f}' if inc and inc.fee_amount is not None else '',
        'incident_count': str(f.get('person.incident_count') or ''),
        'repair_days_out': str(f.get('repair.days_out') or ''),
        'loaner_days_overdue': str(f.get('loaner.days_overdue') or ''),
        'signin_account': ctx['signin']['signin_email'] if ctx.get('signin') else '',
        'signin_flag': ctx['signin']['label'] if ctx.get('signin') else '',
        'warranty_days_left': str(f.get('device.warranty_days_left') or ''),
        'today': date.today().isoformat(),
        'period': ctx['period']['label'] if ctx.get('period') else '',
        'part_name': ctx['part'].name if ctx.get('part') else '',
        'part_on_hand': str(ctx['part'].quantity_on_hand) if ctx.get('part') else '',
        'part_reorder_level': str(ctx['part'].reorder_level) if ctx.get('part') else '',
    }


PLACEHOLDER_NAMES = ['ticket_id', 'ticket_subject', 'ticket_priority', 'ticket_status', 'ticket_category',
                     'requester_name', 'asset_tag', 'serial_number', 'device_model', 'device_site', 'holder_name',
                     'holder_email', 'person_name', 'person_email', 'person_first_name', 'guardian_name',
                     'incident_description', 'fee_amount', 'incident_count', 'repair_days_out', 'loaner_days_overdue',
                     'signin_account', 'signin_flag', 'warranty_days_left', 'part_name', 'part_on_hand',
                     'part_reorder_level', 'period', 'today']
