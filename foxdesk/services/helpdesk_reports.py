"""Help desk numbers: how fast tickets get a first reply and get fixed, by
tech, category and school, how often the SLA targets were met, and how the
open backlog has moved week to week.

Times are school hours (the SLA settings' working days and hours), so a
ticket opened Friday afternoon and answered Monday morning counts as about
an hour, not a weekend. Merged tickets are left out — their history lives
on the ticket they were merged into. Like the dashboard charts, the
bucketing is done in Python: the row counts are small and date truncation
differs between SQLite and Postgres.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import Site, Team, Ticket, TicketCategory, User
from foxdesk.services.scoping import _scope_tickets
from foxdesk.services.sla import Calendar, sla_settings

RANGES = [('30', 'Last 30 days'), ('90', 'Last 90 days'), ('school_year', 'This school year'), ('365', 'Last 12 months')]


def period_start(range_key, now=None):
    now = now or utcnow()
    if range_key == 'school_year':
        year = now.year if now.month >= 7 else now.year - 1
        return datetime(year, 7, 1)
    days = int(range_key) if str(range_key).isdigit() else 30
    return now - timedelta(days=days)


def hours_label(hours):
    if hours is None:
        return '—'
    if hours < 1:
        return f'{max(1, round(hours * 60))} min'
    return f'{hours:.1f} h'


def _sla_outcome(due, done_at, now):
    """'met', 'missed', or None (no target, or still within it)."""
    if not due:
        return None
    if done_at:
        return 'met' if done_at <= due else 'missed'
    return 'missed' if now > due else None


class _Group:
    def __init__(self, label):
        self.label = label
        self.opened = self.resolved = self.open_now = 0
        self.reply_hours, self.fix_hours = [], []
        self.sla = defaultdict(int)

    def as_row(self):
        judged = self.sla['met'] + self.sla['missed']
        return dict(label=self.label, opened=self.opened, resolved=self.resolved, open_now=self.open_now,
                    reply=hours_label(median(self.reply_hours)) if self.reply_hours else '—',
                    fix=hours_label(median(self.fix_hours)) if self.fix_hours else '—',
                    sla=f'{round(self.sla["met"] * 100 / judged)}%' if judged else '—', judged=judged)


def _tickets(site_ids, since):
    """Every ticket the report touches: opened or resolved since `since`,
    or still open."""
    return (_scope_tickets(Ticket.query, site_ids)
            .filter(Ticket.merged_into_id.is_(None),
                    db.or_(Ticket.created_at >= since, Ticket.resolved_at >= since,
                           Ticket.status.in_(['open', 'in_progress'])))
            .all())


def build_helpdesk_report(site_ids, range_key='30', now=None):
    now = now or utcnow()
    since = period_start(range_key, now)
    cal = Calendar(sla_settings())
    users = {u.id: u.username for u in User.query.all()}
    categories = {c.id: c.name for c in TicketCategory.query.all()}
    sites = {s.id: s.name for s in Site.query.all()}
    teams = {t.id: t.name for t in Team.query.all()}

    overall = _Group('All tickets')
    by = {'tech': {}, 'category': {}, 'school': {}}
    reply_sla, fix_sla = defaultdict(int), defaultdict(int)
    rows = []

    def groups(t):
        keys = {'tech': users.get(t.assigned_to_user_id) or (f'{teams[t.team_id]} (team)' if t.team_id in teams else 'Unassigned'),
                'category': categories.get(t.category_id, '—'), 'school': sites.get(t.site_id, 'No school')}
        out = [overall]
        for dim, label in keys.items():
            out.append(by[dim].setdefault(label, _Group(label)))
        return out

    for t in _tickets(site_ids, since):
        targets = groups(t)
        is_open = t.status in ('open', 'in_progress')
        opened_in = t.created_at >= since
        resolved_in = bool(t.resolved_at and t.resolved_at >= since and not is_open)
        reply = cal.minutes_between(t.created_at, t.first_response_at) / 60 if t.first_response_at else None
        fix = cal.minutes_between(t.created_at, t.resolved_at) / 60 if resolved_in else None
        reply_outcome = _sla_outcome(t.response_due_at, t.first_response_at, now) if opened_in else None
        fix_outcome = _sla_outcome(t.resolve_due_at, t.resolved_at if not is_open else None, now) if opened_in else None
        for g in targets:
            g.opened += int(opened_in)
            g.resolved += int(resolved_in)
            g.open_now += int(is_open)
            if opened_in and reply is not None:
                g.reply_hours.append(reply)
            if fix is not None:
                g.fix_hours.append(fix)
            if fix_outcome:
                g.sla[fix_outcome] += 1
        if reply_outcome:
            reply_sla[reply_outcome] += 1
        if fix_outcome:
            fix_sla[fix_outcome] += 1
        if opened_in:
            rows.append(dict(ticket=t, reply=reply, fix=fix, reply_sla=reply_outcome, fix_sla=fix_outcome,
                             category=categories.get(t.category_id, ''), school=sites.get(t.site_id, ''),
                             tech=users.get(t.assigned_to_user_id, ''), team=teams.get(t.team_id, '')))

    def pct(counts):
        judged = counts['met'] + counts['missed']
        return (round(counts['met'] * 100 / judged), judged) if judged else (None, 0)

    def ordered(groups_by_label):
        return sorted((g.as_row() for g in groups_by_label.values()), key=lambda r: (-r['opened'], r['label'].lower()))

    totals = overall.as_row()
    return dict(
        since=since, totals=totals, reply_sla=pct(reply_sla), fix_sla=pct(fix_sla),
        by_tech=ordered(by['tech']), by_category=ordered(by['category']), by_school=ordered(by['school']),
        rows=sorted(rows, key=lambda r: r['ticket'].id), charts=weekly_charts(site_ids, now),
        sla_enabled=sla_settings().enabled,
    )


def weekly_charts(site_ids, now=None, weeks=12):
    """Open backlog at the end of each week, and tickets resolved each week,
    as specs for static/js/charts.js."""
    now = now or utcnow()
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    ends = [min(s + timedelta(weeks=1), now) for s in starts]
    tickets = (_scope_tickets(Ticket.query, site_ids)
               .filter(Ticket.merged_into_id.is_(None), Ticket.created_at < now,
                       db.or_(Ticket.resolved_at.is_(None), Ticket.resolved_at >= starts[0]))
               .with_entities(Ticket.created_at, Ticket.resolved_at, Ticket.status).all())
    backlog, resolved = [], []
    for start, end in zip(starts, ends):
        backlog.append(sum(1 for created, done, status in tickets
                           if created < end and (done is None or done >= end) and (done is not None or status in ('open', 'in_progress'))))
        resolved.append(sum(1 for _, done, status in tickets if done and start <= done < end and status in ('resolved', 'closed')))

    def label(d):
        return f'{d:%b} {d.day}'
    return [
        {'id': 'hd_backlog', 'type': 'columns', 'title': 'Open tickets at the end of each week',
         'subtitle': f'Last {weeks} weeks; the last bar is now', 'unit': 'ticket', 'series': [{'name': 'Open tickets'}],
         'rows': [{'label': label(s), 'tip': f'End of the week of {s:%b %d}', 'values': [v]} for s, v in zip(starts, backlog)]},
        {'id': 'hd_resolved', 'type': 'columns', 'title': 'Tickets resolved each week',
         'subtitle': f'Last {weeks} weeks', 'unit': 'ticket', 'series': [{'name': 'Resolved'}],
         'rows': [{'label': label(s), 'tip': f'Week of {s:%b %d}', 'values': [v]} for s, v in zip(starts, resolved)]},
    ]
