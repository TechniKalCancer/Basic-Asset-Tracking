"""Every charge in one place: damage fees, ticket charges and protection
plans, with what's owed, collected and waived.

The three live in different tables (Incident, TicketCharge,
AgreementSignature); charges() turns them into one list of Charge rows the
Payments pages can filter, total and act on together. A charge is owed
until it's paid (at the office — cash, check, card — or online through
Stripe) or waived; a waived charge stays on record with who waived it and
why, so it can be reopened.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from flask import url_for

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import (
    PAYMENT_METHODS, AgreementSignature, AssetRegistry, FeePayment, Incident, Person, Ticket, TicketCharge,
)
from foxdesk.services.auth import _log_activity

KINDS = {'incident': 'Damage fee', 'ticket': 'Ticket charge', 'plan': 'Protection plan'}
AGE_BUCKETS = [(30, '0–30 days'), (60, '31–60 days'), (90, '61–90 days'), (None, 'Over 90 days')]


@dataclass
class Charge:
    kind: str
    id: int
    person_id: int
    person_name: str
    description: str
    amount: Decimal
    created_at: datetime
    status: str            # owed | paid | waived
    paid_at: datetime = None
    paid_method: str = None
    waived_at: datetime = None
    waived_by: str = None
    waive_reason: str = None
    site_id: int = None
    link_url: str = None
    link_label: str = None
    obj: object = field(default=None, repr=False)

    @property
    def key(self):
        return f'{self.kind}:{self.id}'

    @property
    def kind_label(self):
        return KINDS[self.kind]

    @property
    def age_days(self):
        return (utcnow() - self.created_at).days

    @property
    def method_label(self):
        return PAYMENT_METHODS.get(self.paid_method, self.paid_method or '')


def _status(paid_at, waived_at):
    return 'paid' if paid_at else ('waived' if waived_at else 'owed')


def charges(site_ids=None, include_tickets=True, include_plans=True):
    """Every charge the current user may see, newest first."""
    from foxdesk.services.scoping import _scope_tickets
    out = []
    incidents = Incident.query.filter(Incident.fee_charged.is_(True), Incident.fee_amount > 0)
    if site_ids is not None:
        incidents = incidents.join(AssetRegistry, AssetRegistry.asset_tag == Incident.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids or [-1]))
    sites = dict(db.session.query(AssetRegistry.asset_tag, AssetRegistry.site_id).all())
    for i in incidents:
        out.append(Charge('incident', i.id, i.person_id, i.person_name or '(no one on file)', i.description,
                          Decimal(i.fee_amount), i.created_at, _status(i.paid_at, i.waived_at), i.paid_at, i.paid_method,
                          i.waived_at, i.waived_by, i.waive_reason, sites.get(i.asset_tag),
                          url_for('admin_asset_assign', asset_tag=i.asset_tag), i.asset_tag, i))
    if include_tickets:
        for c, t in _scope_tickets(db.session.query(TicketCharge, Ticket).join(Ticket, Ticket.id == TicketCharge.ticket_id),
                                   site_ids).all():
            out.append(Charge('ticket', c.id, t.requester_person_id, t.requester_name or '(no requester)',
                              f'{c.description} — {t.subject}', Decimal(c.amount), c.created_at,
                              _status(c.paid_at, c.waived_at), c.paid_at, c.paid_method, c.waived_at, c.waived_by,
                              c.waive_reason, t.site_id, url_for('admin_ticket_detail', ticket_id=t.id), f'#{t.id}', c))
    if include_plans:
        plans = (db.session.query(AgreementSignature, Person).join(Person, Person.id == AgreementSignature.person_id)
                 .filter(AgreementSignature.plan_choice == 'enrolled', AgreementSignature.plan_amount > 0))
        if site_ids is not None:
            plans = plans.filter(Person.site_id.in_(site_ids or [-1]))
        paid_online = {sid for (sid,) in db.session.query(FeePayment.signature_id)
                       .filter(FeePayment.status == 'paid', FeePayment.signature_id.isnot(None))}
        for s, p in plans:
            out.append(Charge('plan', s.id, p.id, p.full_name, s.agreement.plan_name or 'Protection plan',
                              Decimal(s.plan_amount), s.signed_at, _status(s.plan_paid_at, s.plan_waived_at),
                              s.plan_paid_at, 'online' if s.id in paid_online else None,
                              s.plan_waived_at, None, None, p.site_id,
                              url_for('admin_agreement_detail', agreement_id=s.agreement_id), s.agreement.title, s))
    return sorted(out, key=lambda c: c.created_at, reverse=True)


def by_keys(rows, keys):
    wanted = set(keys)
    return [c for c in rows if c.key in wanted]


# ─── actions ──────────────────────────────────────────────────────────────────

def mark_paid(rows, method, note, actor):
    """Record payment for every owed charge in rows. Returns how many. Does not commit."""
    from foxdesk.services.agreements import mark_plan_paid
    method = method if method in PAYMENT_METHODS else 'other'
    now, done, total = utcnow(), 0, Decimal('0')
    for c in rows:
        if c.status != 'owed':
            continue
        if c.kind == 'plan':
            mark_plan_paid(c.obj, f'{PAYMENT_METHODS[method].lower()} (recorded by {actor})')
        else:
            c.obj.paid_at, c.obj.paid_method, c.obj.paid_note = now, method, (note or '').strip()[:120] or None
        done += 1
        total += c.amount
    if done:
        _log_activity('fee_paid', f'Recorded {done} payment(s), ${total:.2f} total, by {PAYMENT_METHODS[method].lower()}'
                                  + (f' ({note.strip()})' if (note or '').strip() else '') + '.')
    return done


def waive(rows, reason, actor):
    """Waive every owed charge in rows. Returns how many. Does not commit."""
    now, done, total = utcnow(), 0, Decimal('0')
    reason = (reason or '').strip()[:255] or None
    for c in rows:
        if c.status != 'owed':
            continue
        if c.kind == 'plan':
            c.obj.plan_waived_at = now
        else:
            c.obj.waived_at, c.obj.waived_by, c.obj.waive_reason = now, actor[:160], reason
        done += 1
        total += c.amount
    if done:
        _log_activity('fee_edit', f'Waived {done} charge(s), ${total:.2f} total' + (f': {reason}' if reason else '') + '.')
    return done


def reopen(rows, actor):
    """Undo a waiver: the charge is owed again. Returns how many. Does not commit."""
    done = 0
    for c in rows:
        if c.status != 'waived':
            continue
        if c.kind == 'plan':
            c.obj.plan_waived_at = None
        else:
            c.obj.waived_at = c.obj.waived_by = c.obj.waive_reason = None
        done += 1
    if done:
        _log_activity('fee_edit', f'Reopened {done} waived charge(s) ({actor}).')
    return done


# ─── dashboard ────────────────────────────────────────────────────────────────

def school_year_start(now=None):
    now = now or utcnow()
    return datetime(now.year if now.month >= 7 else now.year - 1, 7, 1)


def summary(rows, sites=None):
    """The numbers on the Payments dashboard."""
    now = utcnow()
    month_start, year_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), school_year_start(now)
    owed = [c for c in rows if c.status == 'owed']
    paid = [c for c in rows if c.status == 'paid']
    waived = [c for c in rows if c.status == 'waived']

    def total(items):
        return sum((c.amount for c in items), Decimal('0'))
    aging = []
    lower = 0
    for upper, label in AGE_BUCKETS:
        items = [c for c in owed if c.age_days >= lower and (upper is None or c.age_days <= upper)]
        aging.append(dict(label=label, count=len(items), amount=total(items)))
        lower = (upper or 0) + 1
    people = defaultdict(lambda: dict(amount=Decimal('0'), count=0, person_id=None))
    for c in owed:
        entry = people[c.person_name]
        entry['amount'] += c.amount
        entry['count'] += 1
        entry['person_id'] = entry['person_id'] or c.person_id
    site_names = {s.id: s.name for s in (sites or [])}
    by_school = defaultdict(lambda: dict(owed=Decimal('0'), collected=Decimal('0')))
    for c in owed:
        by_school[site_names.get(c.site_id, 'No school')]['owed'] += c.amount
    for c in paid:
        if c.paid_at and c.paid_at >= year_start:
            by_school[site_names.get(c.site_id, 'No school')]['collected'] += c.amount
    by_kind = {k: dict(label=label, owed=total(c for c in owed if c.kind == k),
                       collected=total(c for c in paid if c.kind == k and c.paid_at and c.paid_at >= year_start))
               for k, label in KINDS.items()}
    by_method = defaultdict(lambda: Decimal('0'))
    for c in paid:
        if c.paid_at and c.paid_at >= year_start:
            by_method[c.method_label or 'Not recorded'] += c.amount
    recent = sorted([c for c in rows if c.status != 'owed'], key=lambda c: c.paid_at or c.waived_at, reverse=True)[:10]
    return dict(
        owed_total=total(owed), owed_count=len(owed), people_owing=len(people),
        collected_month=total(c for c in paid if c.paid_at and c.paid_at >= month_start),
        collected_year=total(c for c in paid if c.paid_at and c.paid_at >= year_start),
        waived_year=total(c for c in waived if c.waived_at and c.waived_at >= year_start),
        aging=aging, by_kind=by_kind, by_method=sorted(by_method.items(), key=lambda kv: -kv[1]),
        by_school=sorted(by_school.items()), year_start=year_start, recent=recent,
        top_people=sorted(people.items(), key=lambda kv: -kv[1]['amount'])[:10],
    )


def aging_chart(aging):
    return {'id': 'pay_aging', 'type': 'hbar', 'title': 'Owed, by how long', 'subtitle': 'Charges not yet paid or waived',
            'money': True, 'series': [{'name': 'Owed'}],
            'rows': [{'label': b['label'], 'tip': f'{b["count"]} charge{"s" if b["count"] != 1 else ""}',
                      'values': [float(b['amount'])]} for b in aging]}
