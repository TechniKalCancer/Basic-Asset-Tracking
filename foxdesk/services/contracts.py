"""Licenses, subscriptions and contracts with end dates, and when to remind
someone to renew.

A reminder goes into the daily tech email when a contract crosses one of
its reminder points (its own notice_days, then 30, 14, 7 and 1 day out, and
the day it ends). "Crosses" is measured since the previous email, so a point
that falls on a weekend or holiday is caught on the next school day instead
of being skipped.
"""
from datetime import timedelta

from foxdesk.clock import utcnow
from foxdesk.models import CONTRACT_KINDS, Contract

REMIND_POINTS = (30, 14, 7, 1, 0)


def status_of(contract, today=None):
    """(key, label) for the list: ended, renewing (inside the notice window),
    active, or no end date."""
    today = today or utcnow().date()
    if not contract.ends_on:
        return 'open', 'No end date'
    left = (contract.ends_on - today).days
    if left < 0:
        return 'ended', f'Ended {-left} day{"s" if left != -1 else ""} ago'
    if left <= (contract.notice_days or 0):
        return 'renewing', 'Ends today' if left == 0 else f'{left} day{"s" if left != 1 else ""} left'
    return 'active', f'Until {contract.ends_on:%b %-d, %Y}'


def points_for(contract):
    return sorted({p for p in REMIND_POINTS + ((contract.notice_days,) if contract.notice_days else ()) if p >= 0}, reverse=True)


def due_reminders(today=None, previous=None):
    """Active contracts that crossed a reminder point between the previous
    email (exclusive) and today (inclusive): list of dict(contract, days_left)."""
    today = today or utcnow().date()
    previous = previous if previous and previous < today else today - timedelta(days=1)
    out = []
    for c in Contract.query.filter(Contract.is_active.is_(True), Contract.ends_on.isnot(None)):
        left_now, left_before = (c.ends_on - today).days, (c.ends_on - previous).days
        if any(left_now <= p < left_before for p in points_for(c)):
            out.append(dict(contract=c, days_left=left_now))
    return sorted(out, key=lambda d: d['days_left'])


def kind_label(contract):
    return CONTRACT_KINDS.get(contract.kind, contract.kind)
