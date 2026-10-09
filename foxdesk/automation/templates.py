"""Ready-made rules a district can start from (Automations → Templates).

Each template is a normal rule definition. Where it needs something only
the district has (a ticket category, a tech, a repair category), it takes
a best guess from the district's own data — a category named like
"Hardware" or "Cryptohome", the first active tech — and the builder opens
with those prefilled so they can be changed before saving.
"""
from foxdesk.models import RepairCategory, TicketCategory, User


def _category(*hints):
    cats = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    for hint in hints:
        for c in cats:
            if hint in c.name.lower():
                return c.id
    return cats[0].id if cats else None


def _repair_category(*hints):
    cats = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    for hint in hints:
        for c in cats:
            if hint in c.name.lower():
                return c.id
    return None


def _first_tech():
    u = User.query.filter_by(is_active=True).order_by(User.username).first()
    return u.id if u else None


def _c(field, op, value=None):
    return {'field': field, 'op': op, 'value': value}


def _a(kind, **params):
    return {'type': kind, 'params': params}


TEMPLATES = [
    dict(key='cryptohome_profile_clear', group='Help desk',
         name='Cryptohome error → clear the Chromebook\'s profiles',
         description='When a ticket in your Cryptohome/sign-in-error category has a device, stage a profile clear for a tech to confirm.',
         build=lambda: dict(trigger='ticket.created',
                            conditions=[_c('ticket.category', 'eq', _category('cryptohome', 'sign-in', 'login')),
                                        _c('ticket.has_device', 'true')],
                            actions=[_a('profile_clear', confirm=True)])),
    dict(key='broken_screen_priority', group='Help desk',
         name='Broken screens jump the queue',
         description='Tickets that mention a cracked or broken screen become high priority.',
         build=lambda: dict(trigger='ticket.created', match='any',
                            conditions=[_c('ticket.subject', 'contains', 'screen'),
                                        _c('ticket.description', 'contains', 'cracked')],
                            actions=[_a('set_priority', value='high')])),
    dict(key='urgent_to_chat', group='Help desk',
         name='Post urgent tickets to Teams or Slack',
         description='Every new urgent ticket goes to your tech channel through an incoming webhook.',
         build=lambda: dict(trigger='ticket.created',
                            conditions=[_c('ticket.priority', 'eq', 'urgent')],
                            actions=[_a('post_webhook', url='',
                                        message='Urgent ticket #{ticket_id}: {ticket_subject} — {requester_name} '
                                                '({device_site})')])),
    dict(key='assign_by_school', group='Help desk',
         name='Assign each school\'s tickets to its tech',
         description='Tickets from one school go straight to that school\'s tech. Make one rule per school.',
         build=lambda: dict(trigger='ticket.created',
                            conditions=[_c('ticket.site', 'eq', None)],
                            actions=[_a('assign_ticket', value=_first_tech())])),
    dict(key='damage_escalation', group='Damage and fees',
         name='Second damage report → fee and parent notice',
         description='A student\'s 2nd (or later) damage report without the protection plan gets the standard fee '
                     'and an email to their parent/guardian. The first one is free.',
         build=lambda: dict(trigger='incident.created',
                            conditions=[_c('person.incident_count', 'gte', '2'),
                                        _c('person.has_protection_plan', 'false'),
                                        _c('person.role', 'eq', 'student')],
                            actions=[_a('set_incident_fee', amount='45.00'), _a('notify_guardian')])),
    dict(key='damage_ticket', group='Damage and fees',
         name='Damage report → repair ticket',
         description='Every damage report opens a ticket for the tech team with the device attached.',
         build=lambda: dict(trigger='incident.created', conditions=[],
                            actions=[_a('create_ticket', category=_category('hardware', 'repair', 'damage'),
                                        subject='Damage: {asset_tag} — {incident_description}',
                                        description='{person_name} reported: {incident_description}',
                                        priority='normal', assignee=None)])),
    dict(key='repair_overdue', group='Repairs',
         name='Repair out more than 14 days → chase the vendor',
         description='Once a repair has been out two weeks, email your tech team to follow up.',
         build=lambda: dict(trigger='repair.overdue',
                            conditions=[_c('repair.days_out', 'gte', '14')],
                            actions=[_a('send_email', to='address', address='',
                                        subject='Repair still out: {asset_tag} ({repair_days_out} days)',
                                        body='{asset_tag} ({device_model}) has been out for repair for '
                                             '{repair_days_out} days. Time to check with the vendor.')])),
    dict(key='repair_back_notify', group='Repairs',
         name='Repair is back → tell the student',
         description='When a repaired device comes back fixed, email whoever it belongs to.',
         build=lambda: dict(trigger='repair.returned',
                            conditions=[_c('repair.outcome', 'eq', 'fixed')],
                            actions=[_a('send_email', to='holder', address='',
                                        subject='Your device {asset_tag} is fixed',
                                        body='Hi {person_first_name},\n\nYour device {asset_tag} is back from repair. '
                                             'Pick it up from the tech office.\n\nThanks!')])),
    dict(key='loaner_overdue_ticket', group='Loaners',
         name='Loaner a week overdue → ticket and disable',
         description='A loaner 7+ days overdue opens a ticket and stages a Google disable for a tech to confirm.',
         build=lambda: dict(trigger='loaner.overdue',
                            conditions=[_c('loaner.days_overdue', 'gte', '7')],
                            actions=[_a('create_ticket', category=_category('loaner', 'hardware'),
                                        subject='Overdue loaner {asset_tag} — {person_name}',
                                        description='{asset_tag} is {loaner_days_overdue} days overdue.',
                                        priority='normal', assignee=None),
                                     _a('disable_google', confirm=True)])),
    dict(key='withdrawn_collect', group='People',
         name='Student leaves with a device → collection ticket',
         description='When a student is marked graduated/withdrawn while still holding a device, open a ticket to collect it.',
         build=lambda: dict(trigger='person.deactivated',
                            conditions=[_c('person.device_count', 'gte', '1')],
                            actions=[_a('create_ticket', category=_category('collection', 'hardware'),
                                        subject='Collect devices from {person_name}',
                                        description='{person_name} ({person_email}) left but still holds a device.',
                                        priority='high', assignee=None)])),
    dict(key='device_aup_email', group='People',
         name='Device handed out → acceptable-use email to the family',
         description='When a student is assigned a device, email their parent/guardian the device agreement.',
         build=lambda: dict(trigger='device.assigned',
                            conditions=[_c('person.role', 'eq', 'student'), _c('device.is_loaner', 'false')],
                            actions=[_a('send_email', to='guardian', address='',
                                        subject='{person_first_name} has been issued school device {asset_tag}',
                                        body='Dear {guardian_name},\n\n{person_name} has been issued device {asset_tag} '
                                             '({device_model}). Please review the district\'s device agreement with '
                                             'your student.\n\nThank you.')])),
    dict(key='lost_turned_up', group='Devices',
         name='A lost device turns up → urgent ticket',
         description='If the Google sign-in check sees a device marked lost or retired being used, open an urgent ticket.',
         build=lambda: dict(trigger='signin.flagged',
                            conditions=[_c('signin.category', 'eq', 'lost_in_use')],
                            actions=[_a('create_ticket', category=_category('lost', 'hardware'),
                                        subject='Lost device in use: {asset_tag}',
                                        description='{signin_account} signed in to {asset_tag}, which is marked lost/retired.',
                                        priority='urgent', assignee=None)])),
    dict(key='warranty_expiring', group='Devices',
         name='Warranty ends in 30 days → heads-up email',
         description='One email per device a month before its warranty runs out.',
         build=lambda: dict(trigger='device.warranty_expiring',
                            conditions=[_c('device.warranty_days_left', 'lte', '30')],
                            actions=[_a('send_email', to='address', address='',
                                        subject='Warranty ending: {asset_tag} in {warranty_days_left} days',
                                        body='{asset_tag} ({device_model}, {device_site}) warranty ends in '
                                             '{warranty_days_left} days.')])),
    dict(key='weekly_summary', group='Reports',
         name='Weekly summary every Monday morning',
         description='Tickets, damage and fees, repairs, loaners and devices for last week, emailed Monday at 7 AM. '
                     'Make one per principal with "Only at" set to their school.',
         build=lambda: dict(trigger='schedule.weekly',
                            conditions=[_c('schedule.weekday', 'eq', 'monday'), _c('schedule.hour', 'gte', '7')],
                            actions=[_a('send_report', report='summary', to='', site=None)])),
    dict(key='daily_overdue', group='Reports',
         name='Overdue list every school-day morning',
         description='Overdue loaners, repairs out 14+ days and devices past their return date, to the tech team '
                     'at 7 AM Monday to Friday.',
         build=lambda: dict(trigger='schedule.daily',
                            conditions=[_c('schedule.weekday', 'in', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']),
                                        _c('schedule.hour', 'gte', '7')],
                            actions=[_a('send_report', report='overdue', to='', site=None)])),
    dict(key='monthly_damage', group='Reports',
         name='Monthly damage and fees report',
         description='Last month\'s damage reports and everything still unpaid, on the 1st of each month.',
         build=lambda: dict(trigger='schedule.monthly',
                            conditions=[_c('schedule.hour', 'gte', '7')],
                            actions=[_a('send_report', report='damage', to='', site=None)])),
    dict(key='part_low_email', group='Repairs',
         name='A part runs low → email to reorder',
         description='When a part reaches its reorder level, email whoever orders parts. Once per restock.',
         build=lambda: dict(trigger='part.low_stock', conditions=[],
                            actions=[_a('send_email', to='address', address='',
                                        subject='Reorder: {part_name} ({part_on_hand} left)',
                                        body='{part_name} is down to {part_on_hand} (reorder level '
                                             '{part_reorder_level}).')])),
]
TEMPLATES_BY_KEY = {t['key']: t for t in TEMPLATES}
