"""Report emails for the "Email a report" automation action: plain-text
summaries of a period (yesterday, last week, last month), for one school or
the whole district."""
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal

from foxdesk.core import APP_URL, db
from foxdesk.models import (
    Asset, AssetRegistry, AssignmentHistory, Incident, LoanerCheckout, Part, Repair, Site, Ticket,
)
from foxdesk.services.features import feature_enabled

REPORTS = OrderedDict([
    ('summary', 'Summary: tickets, damage, repairs, loaners and devices'),
    ('overdue', 'Overdue: loaners, long repairs, devices past their due date'),
    ('tickets', 'Open tickets'),
    ('damage', 'Damage reports and unpaid fees'),
])
REPAIR_LONG_DAYS = 14


def _app_name():
    try:
        from foxdesk.models import BrandingSettings
        row = BrandingSettings.query.get(1)
        return (row.app_name if row and row.app_name else None) or 'FoxDesk'
    except Exception:
        return 'FoxDesk'


def _or_none(items):
    return items or ['  None']


def _money(value):
    return f'${Decimal(value or 0):,.2f}'


def _site_tags(site_id):
    """A subquery of asset tags at the site, for tables keyed by asset_tag."""
    return db.select(AssetRegistry.asset_tag).where(AssetRegistry.site_id == site_id)


def _scoped(query, model, site_id):
    if not site_id:
        return query
    if model is Ticket:
        return query.filter(Ticket.site_id == site_id)
    return query.filter(model.asset_tag.in_(_site_tags(site_id)))


def build_report(kind, start, end, site_id=None, period_label=''):
    """(subject, body) for one report over [start, end) — naive UTC datetimes."""
    site = Site.query.get(site_id) if site_id else None
    where = site.name if site else 'all schools'
    title = REPORTS.get(kind, kind).split(':')[0]
    subject = f'{_app_name()} {title.lower()}, {period_label} ({where})'
    builder = {'summary': _summary, 'overdue': _overdue, 'tickets': _tickets, 'damage': _damage}[kind]
    lines = [f'{_app_name()}: {title.lower()} for {period_label}, {where}', '']
    lines += builder(start, end, site_id)
    if APP_URL:
        lines += ['', f'Open {_app_name()}: {APP_URL}/admin']
    return subject, '\n'.join(lines).rstrip() + '\n'


def _summary(start, end, site_id):
    now = datetime.utcnow()
    t = _scoped(Ticket.query, Ticket, site_id)
    opened = t.filter(Ticket.created_at >= start, Ticket.created_at < end).count()
    resolved = t.filter(Ticket.resolved_at >= start, Ticket.resolved_at < end).count()
    open_now = t.filter(Ticket.status.in_(('open', 'in_progress'))).all()
    by_priority = Counter(x.priority for x in open_now)
    oldest = min(open_now, key=lambda x: x.created_at, default=None)
    lines = ['TICKETS',
             f'  Opened: {opened}   Resolved: {resolved}   Open now: {len(open_now)}'
             + (f" ({', '.join(f'{n} {p}' for p, n in by_priority.items() if p in ('urgent', 'high'))})"
                if by_priority.get('urgent') or by_priority.get('high') else '')]
    if oldest:
        lines.append(f'  Oldest open: #{oldest.id} "{oldest.subject}", {(now - oldest.created_at).days} days')

    if feature_enabled('incidents'):
        inc = _scoped(Incident.query, Incident, site_id)
        new = inc.filter(Incident.created_at >= start, Incident.created_at < end).all()
        unpaid = inc.filter(Incident.fee_charged.is_(True), Incident.paid_at.is_(None)).all()
        lines += ['', 'DAMAGE AND FEES',
                  f'  Damage reports: {len(new)}   Fees charged: {_money(sum(i.fee_amount or 0 for i in new if i.fee_charged))}',
                  f'  Unpaid fees (all time): {_money(sum(i.fee_amount or 0 for i in unpaid))} on {len(unpaid)} report(s)']

    if feature_enabled('repairs'):
        rep = _scoped(Repair.query, Repair, site_id)
        out = rep.filter(Repair.returned_at.is_(None)).all()
        lines += ['', 'REPAIRS',
                  f'  Sent: {rep.filter(Repair.sent_at >= start, Repair.sent_at < end).count()}'
                  f'   Back: {rep.filter(Repair.returned_at >= start, Repair.returned_at < end).count()}'
                  f'   Out now: {len(out)}'
                  f' ({sum(1 for r in out if (now - r.sent_at).days >= REPAIR_LONG_DAYS)} out {REPAIR_LONG_DAYS}+ days)']

    if feature_enabled('loaners'):
        loans = _scoped(LoanerCheckout.query, LoanerCheckout, site_id).filter(LoanerCheckout.checked_in_at.is_(None)).all()
        overdue = [l for l in loans if l.due_date and l.due_date < now.date()]
        lines += ['', 'LOANERS', f'  Out now: {len(loans)}   Overdue: {len(overdue)}']

    devices = AssetRegistry.query.join(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
    if site_id:
        devices = devices.filter(AssetRegistry.site_id == site_id)
    statuses = Counter(s or 'available' for (s,) in devices.with_entities(Asset.status).all())
    in_use_unassigned = devices.filter(Asset.assigned_to_id.is_(None), Asset.google_last_activity >= now - timedelta(days=7),
                                       AssetRegistry.is_loaner.isnot(True)).count()
    lines += ['', 'DEVICES',
              '  ' + ' · '.join(f'{s.replace("_", " ").capitalize()} {n:,}' for s, n in statuses.most_common())]
    if in_use_unassigned:
        lines.append(f'  Used in the last week but not assigned to anyone: {in_use_unassigned}')

    if feature_enabled('parts'):
        low = [p for p in Part.query.filter(Part.is_active.is_(True), Part.reorder_level > 0,
                                            Part.quantity_on_hand <= Part.reorder_level)
               if not site_id or p.site_id in (None, site_id)]
        if low:
            lines += ['', 'PARTS RUNNING LOW'] + [f'  {p.name}: {p.quantity_on_hand} left (reorder at {p.reorder_level})'
                                                 for p in low[:15]]
    return lines


def _overdue(start, end, site_id):
    today = datetime.utcnow().date()
    lines = []
    if feature_enabled('loaners'):
        loans = (_scoped(LoanerCheckout.query, LoanerCheckout, site_id)
                 .filter(LoanerCheckout.checked_in_at.is_(None), LoanerCheckout.due_date < today)
                 .order_by(LoanerCheckout.due_date).all())
        lines += [f'OVERDUE LOANERS ({len(loans)})'] + _or_none(
            [f'  {l.asset_tag}  {l.person_name}, {(today - l.due_date).days} days overdue' for l in loans[:100]]) + ['']
    if feature_enabled('repairs'):
        cutoff = datetime.utcnow() - timedelta(days=REPAIR_LONG_DAYS)
        reps = (_scoped(Repair.query, Repair, site_id).filter(Repair.returned_at.is_(None), Repair.sent_at <= cutoff)
                .order_by(Repair.sent_at).all())
        lines += [f'REPAIRS OUT {REPAIR_LONG_DAYS}+ DAYS ({len(reps)})'] + _or_none(
            [f'  {r.asset_tag}  {(datetime.utcnow() - r.sent_at).days} days'
             + (f', ticket #{r.ticket_id}' if r.ticket_id else '') for r in reps[:100]]) + ['']
    due = (_scoped(AssignmentHistory.query, AssignmentHistory, site_id)
           .filter(AssignmentHistory.unassigned_at.is_(None), AssignmentHistory.due_date < today)
           .order_by(AssignmentHistory.due_date).all())
    return lines + [f'DEVICES PAST THEIR RETURN DATE ({len(due)})'] + _or_none(
        [f'  {h.asset_tag}  {h.person_name}, due {h.due_date:%b %d}' for h in due[:100]])


def _tickets(start, end, site_id):
    now = datetime.utcnow()
    order = {'urgent': 0, 'high': 1, 'normal': 2, 'low': 3}
    tickets = (_scoped(Ticket.query, Ticket, site_id).filter(Ticket.status.in_(('open', 'in_progress'))).all())
    tickets.sort(key=lambda t: (order.get(t.priority, 9), t.created_at))
    return [f'OPEN TICKETS ({len(tickets)})'] + _or_none(
        [f'  #{t.id} [{t.priority}] {t.subject} ({(now - t.created_at).days}d, '
         f'{t.assigned_to.username if t.assigned_to else "unassigned"})' for t in tickets[:150]])


def _damage(start, end, site_id):
    inc = _scoped(Incident.query, Incident, site_id)
    new = inc.filter(Incident.created_at >= start, Incident.created_at < end).order_by(Incident.created_at).all()
    unpaid = inc.filter(Incident.fee_charged.is_(True), Incident.paid_at.is_(None)).order_by(Incident.created_at).all()
    return ([f'DAMAGE REPORTS ({len(new)})'] + _or_none(
        [f'  {i.created_at:%b %d}  {i.asset_tag}  {i.person_name or "unknown"}: {i.description[:70]}'
         + (f'  fee {_money(i.fee_amount)}' if i.fee_charged else '') for i in new[:100]])
        + ['', f'UNPAID FEES ({len(unpaid)}, {_money(sum(i.fee_amount or 0 for i in unpaid))})'] + _or_none(
        [f'  {i.created_at:%b %d}  {i.person_name or "unknown"}  {_money(i.fee_amount)}  ({i.asset_tag})' for i in unpaid[:100]]))
