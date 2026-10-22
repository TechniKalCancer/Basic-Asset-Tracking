"""Receipts for payments, and invoices for what a student's family owes.

A receipt is issued every time charges are marked paid — at the office
(services/billing.mark_paid), or online (services/payments.confirm) — one per
person per payment, and each charge remembers its receipt (receipt_id, or
plan_receipt_id on a protection plan). Payments recorded before receipts
existed can get one afterwards (receipts_for_earlier), dated when they were
paid.

An invoice isn't stored: it's what the student owes right now, printed for
home. Neither shows anything but the charged amounts.
"""
from collections import OrderedDict
from decimal import Decimal

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import InvoiceSettings, PAYMENT_METHODS, Person, Receipt
from foxdesk.services.auth import _log_activity


def invoice_settings(create=False):
    row = db.session.get(InvoiceSettings, 1)
    if row is None:
        row = InvoiceSettings(id=1)
        if create:
            db.session.add(row)
    return row


def _item(c):
    return dict(kind=c.kind_label, description=c.description, ref=c.link_label or '', amount=f'{c.amount:.2f}')


def _group_by_person(rows):
    groups = OrderedDict()
    for c in rows:
        groups.setdefault(c.person_id or f'name:{c.person_name}', []).append(c)
    return groups.values()


def issue_receipts(rows, method, reference, actor, paid_at=None):
    """One receipt per person for charges just paid (Charge rows), linked
    back from each charge. Does not commit; returns the receipts."""
    receipts = []
    for group in _group_by_person(rows):
        first = group[0]
        person = db.session.get(Person, first.person_id) if first.person_id else None
        receipt = Receipt(person_id=first.person_id, person_name=first.person_name[:160],
                          site_id=(person.site_id if person and person.site_id else None)
                          or next((c.site_id for c in group if c.site_id), None),
                          amount=sum((c.amount for c in group), Decimal('0')),
                          method=method if method in PAYMENT_METHODS else 'other',
                          reference=(reference or '').strip()[:120] or None, received_by=(actor or '')[:160] or None,
                          items=[_item(c) for c in group], paid_at=paid_at or utcnow(), created_at=utcnow())
        db.session.add(receipt)
        db.session.flush()
        for c in group:
            setattr(c.obj, 'plan_receipt_id' if c.kind == 'plan' else 'receipt_id', receipt.id)
        receipts.append(receipt)
    return receipts


def receipts_for_earlier(person_id, actor):
    """Receipts for this person's charges paid before receipts existed: one
    per payment day and method. Does not commit; returns the new receipts."""
    from foxdesk.services.billing import person_charges
    waiting = [c for c in person_charges(person_id) if c.status == 'paid' and not c.receipt_id]
    batches = OrderedDict()
    for c in sorted(waiting, key=lambda c: c.paid_at):
        batches.setdefault((c.paid_at.date(), c.paid_method or 'other'), []).append(c)
    out = []
    for (day, method), group in batches.items():
        out += issue_receipts(group, method, 'Recorded before receipts were issued', actor, group[0].paid_at)
    if out:
        _log_activity('fee_paid', f'Made {len(out)} receipt(s) for earlier payments by {out[0].person_name}.')
    return out


def invoice_for(person, today=None):
    """What a student's family owes: the open charges, oldest first, and
    the year's payments for reference."""
    from foxdesk.services.billing import person_charges, school_year_start
    rows = person_charges(person.id)
    owed = [c for c in rows if c.status == 'owed']
    year_start = school_year_start()
    paid = [c for c in rows if c.status == 'paid' and c.paid_at and c.paid_at >= year_start]
    today = today or utcnow().date()
    return dict(person=person, owed=owed, total=sum((c.amount for c in owed), Decimal('0')),
                paid=paid, paid_total=sum((c.amount for c in paid), Decimal('0')),
                number=f'INV-{person.id}-{today:%y%m%d}', date=today)


def method_label(receipt):
    return PAYMENT_METHODS.get(receipt.method, receipt.method)
