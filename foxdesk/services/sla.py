"""SLA clocks: how fast a ticket should get a reply, and be fixed.

Targets are per priority and counted in school hours — a ticket opened at
3:50 PM Friday with a 1-hour reply target is due at 8:20 AM Monday, not
4:50 PM Friday — using the working days, hours and closed dates on the SLA
settings page, in the district's time zone (APP_TIMEZONE).

Due times are stored on the ticket (response_due_at, resolve_due_at) when
it's opened and whenever its priority changes, so queues can filter on
them in SQL. Only tickets opened while SLAs are on get clocks: turning
them on doesn't make every ticket already in the queue overdue. The reply clock stops at the first reply the requester was
actually sent (or the resolution); the fix clock at resolution.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import g, has_request_context

from foxdesk.clock import utcnow
from foxdesk.core import APP_TIMEZONE, app, db
from foxdesk.models import TICKET_PRIORITIES, SlaSettings, Ticket

DEFAULT_TARGETS = {
    'urgent': {'response': 1, 'resolve': 8},
    'high':   {'response': 2, 'resolve': 16},
    'normal': {'response': 8, 'resolve': 40},
    'low':    {'response': 16, 'resolve': 80},
}
DUE_SOON_MINUTES = 60
WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_DATE = r'(\d{4}-\d{2}-\d{2})'
_RANGE = re.compile(rf'^{_DATE}(?:\s*(?:to|through|-|–)\s*{_DATE})?$')


def _unsaved_settings():
    return SlaSettings(id=1, enabled=False, work_days='0,1,2,3,4', day_start='07:30', day_end='16:00',
                       closed_dates='', targets=DEFAULT_TARGETS)


def sla_settings(create=False):
    """The settings row; until it's first saved, the defaults (off).
    create=True adds the row so the caller can save it."""
    row = db.session.get(SlaSettings, 1)
    if row is None:
        row = _unsaved_settings()
        if create:
            db.session.add(row)
    return row


def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception:
        return ZoneInfo('UTC')


def parse_hhmm(value):
    m = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*', value or '')
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def parse_closed_dates(text):
    """(set of dates, [problem lines]) from one date or range per line;
    anything after # is a note ("2026-11-26 to 2026-11-27  # Thanksgiving")."""
    days, problems = set(), []
    for raw in (text or '').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        m = _RANGE.match(line)
        try:
            first = date.fromisoformat(m.group(1)) if m else None
            last = date.fromisoformat(m.group(2)) if m and m.group(2) else first
        except ValueError:
            first = None
        if first is None or last < first or (last - first).days > 366:
            problems.append(raw.strip())
            continue
        days.update(first + timedelta(days=i) for i in range((last - first).days + 1))
    return days, problems


class Calendar:
    """The school-hours calendar from the SLA settings."""

    def __init__(self, row):
        self.tz = _tz()
        self.days = {int(d) for d in (row.work_days or '').split(',') if d.strip().isdigit()}
        self.start = parse_hhmm(row.day_start) or time(7, 30)
        self.end = parse_hhmm(row.day_end) or time(16, 0)
        self.closed, _ = parse_closed_dates(row.closed_dates)

    def local(self, naive_utc):
        return naive_utc.replace(tzinfo=timezone.utc).astimezone(self.tz)

    @staticmethod
    def utc(aware):
        return aware.astimezone(timezone.utc).replace(tzinfo=None)

    def _window(self, d):
        """(open, close) for a working day, else None."""
        if d.weekday() not in self.days or d in self.closed or self.end <= self.start:
            return None
        return datetime.combine(d, self.start, tzinfo=self.tz), datetime.combine(d, self.end, tzinfo=self.tz)

    def is_school_day(self, d):
        return self._window(d) is not None

    def add_hours(self, start_utc, hours):
        """The naive-UTC moment `hours` school hours after start_utc, or
        None for a blank target (or a calendar with no working time)."""
        if hours in (None, '') or start_utc is None:
            return None
        remaining = float(hours) * 60
        t = self.local(start_utc)
        for _ in range(800):
            window = self._window(t.date())
            if window:
                opens, closes = window
                t = max(t, opens)
                if t < closes:
                    available = (closes - t).total_seconds() / 60
                    if remaining <= available:
                        return self.utc(t + timedelta(minutes=remaining))
                    remaining -= available
            t = datetime.combine(t.date() + timedelta(days=1), time(0), tzinfo=self.tz)
        return None

    def minutes_between(self, a_utc, b_utc):
        """School minutes from a to b (0 if b is before a)."""
        if not a_utc or not b_utc or b_utc <= a_utc:
            return 0
        a, b = self.local(a_utc), self.local(b_utc)
        total, d = 0.0, a.date()
        while d <= b.date() and (d - a.date()).days <= 800:
            window = self._window(d)
            if window:
                lo, hi = max(window[0], a), min(window[1], b)
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60
            d += timedelta(days=1)
        return total


def _cached():
    """(settings, calendar) once per request — list pages ask for every row."""
    if has_request_context():
        hit = getattr(g, '_sla', None)
        if hit is None:
            row = sla_settings()
            hit = g._sla = (row, Calendar(row))
        return hit
    row = sla_settings()
    return row, Calendar(row)


def sla_on():
    return bool(_cached()[0].enabled)


def calendar():
    return _cached()[1]


def target_hours(row, priority, kind):
    value = ((row.targets or {}).get(priority) or {}).get(kind)
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def apply_sla(ticket, row=None, cal=None):
    """Set the ticket's due times from its priority (none when SLAs are off).
    Measured from when the ticket was opened. Does not commit."""
    if row is None:
        row, cal = _cached()
    cal = cal or Calendar(row)
    opened = ticket.created_at or utcnow()
    if not row.enabled or (row.started_at and opened < row.started_at):
        ticket.response_due_at = ticket.resolve_due_at = None
        return
    ticket.response_due_at = cal.add_hours(opened, target_hours(row, ticket.priority, 'response'))
    ticket.resolve_due_at = cal.add_hours(opened, target_hours(row, ticket.priority, 'resolve'))


def reapply_open_tickets():
    """After the settings change: recompute the open tickets opened since
    SLAs were turned on (and clear clocks if they're now off). Returns how
    many tickets have clocks."""
    row = sla_settings()
    cal = Calendar(row)
    count = 0
    for t in Ticket.query.filter(Ticket.status.in_(['open', 'in_progress']), Ticket.merged_into_id.is_(None),
                                 db.or_(Ticket.response_due_at.isnot(None), Ticket.resolve_due_at.isnot(None),
                                        Ticket.created_at >= (row.started_at or utcnow()))):
        apply_sla(t, row, cal)
        count += int(bool(t.response_due_at or t.resolve_due_at))
    if has_request_context():
        g.pop('_sla', None)
    return count


def mark_responded(ticket, when=None):
    """The requester heard back (a reply emailed to them, or the ticket was
    resolved). Only the first one counts. Does not commit."""
    if ticket.first_response_at is None:
        ticket.first_response_at = when or utcnow()


def overdue_filter(now=None):
    """SQL condition: an open ticket that's missed its reply or fix time."""
    now = now or utcnow()
    return db.and_(Ticket.status.in_(['open', 'in_progress']), db.or_(
        db.and_(Ticket.first_response_at.is_(None), Ticket.response_due_at.isnot(None), Ticket.response_due_at < now),
        db.and_(Ticket.resolve_due_at.isnot(None), Ticket.resolve_due_at < now)))


def breach_kind(ticket, now=None):
    """'response' or 'resolve' if an open ticket has missed that clock now."""
    now = now or utcnow()
    if not ticket.is_open:
        return None
    if ticket.first_response_at is None and ticket.response_due_at and ticket.response_due_at < now:
        return 'response'
    if ticket.resolve_due_at and ticket.resolve_due_at < now:
        return 'resolve'
    return None


def when_label(naive_utc, cal=None):
    """'today 2:30 PM', 'Tue 9:30 AM' or 'Oct 14, 9:30 AM', in district time."""
    cal = cal or calendar()
    local, today = cal.local(naive_utc), cal.local(utcnow()).date()
    clock = local.strftime('%I:%M %p').lstrip('0')
    if local.date() == today:
        return f'today {clock}'
    if local.date() == today + timedelta(days=1):
        return f'tomorrow {clock}'
    if today < local.date() <= today + timedelta(days=6):
        return f'{local:%a} {clock}'
    return f'{local:%b} {local.day}, {clock}'


def sla_badge(ticket):
    """What to show for a ticket's SLA right now, or None:
    dict(text, css, title) — css is a badge class."""
    if ticket.merged_into_id:
        return None
    row, cal = _cached()
    now = utcnow()
    if not ticket.is_open:
        if ticket.resolve_due_at and ticket.resolved_at:
            on_time = ticket.resolved_at <= ticket.resolve_due_at
            return dict(text='Fixed on time' if on_time else 'Fixed late', css='badge-green' if on_time else 'badge-red',
                        title=f'Fix was due {when_label(ticket.resolve_due_at, cal)}')
        return None
    if ticket.first_response_at is None and ticket.response_due_at:
        noun, due = 'Reply', ticket.response_due_at
    elif ticket.resolve_due_at:
        noun, due = 'Fix', ticket.resolve_due_at
    else:
        return None
    if due < now:
        return dict(text=f'{noun} overdue', css='badge-red', title=f'{noun} was due {when_label(due, cal)}')
    if cal.minutes_between(now, due) <= DUE_SOON_MINUTES:
        return dict(text=f'{noun} due soon', css='badge-yellow', title=f'{noun} due {when_label(due, cal)}')
    return dict(text=f'{noun} by {when_label(due, cal)}', css='badge-neutral', title=f'{noun} due {when_label(due, cal)}')


app.jinja_env.globals.update(sla_badge=sla_badge, sla_on=sla_on, sla_when=lambda when: when_label(when))
