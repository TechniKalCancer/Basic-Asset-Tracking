"""Spot checks: walk a classroom with a phone, scan a student's device
(and their ID card, or tap who has it), and see at once whether the right
student has it.

A scan can be a device (asset tag, serial, or a FoxDesk label's QR code,
which holds a /r/<tag> link) or a person (student/staff ID number or
email). The device's rightful holder is whoever has it checked out as a
loaner, else whoever it's assigned to. Every check is kept; a problem
(someone else's device, or a device assigned to no one) stays open until
it's marked resolved.
"""
import re
from urllib.parse import unquote

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import Asset, AssetRegistry, LoanerCheckout, Person, SpotCheck
from foxdesk.services.auth import _log_activity

QR_LINK = re.compile(r'/r/([^/?#\s]+)')


def parse_code(raw):
    """The value a scan carries: the tag out of a label's QR link, else the
    text itself, trimmed."""
    code = (raw or '').strip()
    m = QR_LINK.search(code)
    return unquote(m.group(1)).strip() if m and '://' in code else code


def find_device(code, site_ids=None):
    """The registry device a scanned tag or serial belongs to, or None."""
    from foxdesk.services.identities import find_registry_row
    from foxdesk.services.scoping import _scope_registry
    code = parse_code(code)
    if not code:
        return None
    scoped = _scope_registry(AssetRegistry.query, site_ids)
    row = (scoped.filter(AssetRegistry.asset_tag == code).first()
           or scoped.filter(db.func.lower(AssetRegistry.asset_tag) == code.lower()).first()
           or scoped.filter(db.func.lower(AssetRegistry.serial_number) == code.lower()).first())
    if row is None:
        row = find_registry_row(serial_number=code)
        if row is not None and site_ids is not None and row.site_id not in site_ids:
            row = None
    return row


def find_person(code, site_ids=None):
    """The active person a scanned ID number or email belongs to, or None."""
    from foxdesk.services.scoping import _scope_people
    code = parse_code(code)
    if not code:
        return None
    people = _scope_people(Person.query, site_ids).filter(Person.is_active.is_(True))
    return (people.filter(Person.external_id == code).first()
            or people.filter(db.func.lower(Person.external_id) == code.lower()).first()
            or (people.filter(db.func.lower(Person.email) == code.lower()).first() if '@' in code else None))


def expected_holder(asset_tag):
    """(person, 'loaner' | 'assigned') who should have this device, or (None, None)."""
    loan = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).order_by(LoanerCheckout.id.desc()).first()
    if loan and loan.person_id:
        person = db.session.get(Person, loan.person_id)
        if person:
            return person, 'loaner'
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    if asset and asset.assigned_to:
        return asset.assigned_to, 'assigned'
    return None, None


def devices_of(person):
    tags = [a.asset_tag for a in Asset.query.filter_by(assigned_to_id=person.id).order_by(Asset.asset_tag)]
    tags += [l.asset_tag for l in LoanerCheckout.query.filter_by(person_id=person.id, checked_in_at=None)]
    return tags


def person_info(person):
    if person is None:
        return None
    return dict(id=person.id, name=person.full_name, first_name=person.first_name, role=person.role,
                grad_year=person.grad_year, site=person.site.name if person.site else None,
                devices=devices_of(person))


def device_info(row):
    asset = Asset.query.filter_by(asset_tag=row.asset_tag).first()
    expected, how = expected_holder(row.asset_tag)
    signin = None
    if asset and (asset.google_recent_users or asset.google_recent_user):
        email = (asset.google_recent_users or [asset.google_recent_user])[0]
        from foxdesk.services.identities import find_person as find_by_email
        who = find_by_email(email=email)
        signin = dict(email=email, name=who.full_name if who else None, same=bool(expected and who and who.id == expected.id),
                      when=asset.google_last_activity.isoformat() + 'Z' if asset.google_last_activity else None)
    model = (row.device_model.full_name if row.device_model else None) or (asset.google_model if asset else None) \
        or row.description
    return dict(asset_tag=row.asset_tag, model=model, type=row.device_type, is_equipment=row.is_equipment,
                site=row.site.name if row.site else None, status=asset.status if asset else 'available',
                expected=person_info(expected), how=how, signin=signin)


def record_check(asset_tag, row, found, checked_by, site_id=None, note=None):
    """Save one check: `found` is who had the device (None when the tech
    confirmed it's with the right student). Returns the SpotCheck. Commits."""
    expected, _ = expected_holder(asset_tag) if row is not None else (None, None)
    if row is None:
        result = 'unknown_device'
    elif found is None:
        found = expected
        result = 'match' if expected else 'unassigned_in_use'
    elif expected is None:
        result = 'unassigned_in_use'
    else:
        result = 'match' if found.id == expected.id else 'wrong_student'
    check = SpotCheck(asset_tag=asset_tag[:120], expected_person_id=expected.id if expected else None,
                      found_person_id=found.id if found else None, result=result, note=(note or '').strip() or None,
                      site_id=(row.site_id if row is not None else None) or site_id, checked_by=checked_by[:160])
    db.session.add(check)
    if result != 'match':
        who = found.full_name if found else 'someone'
        _log_activity('spot_check', f'Spot check: {asset_tag} — {check.result_label.lower()}'
                                    + (f' (with {who}' + (f', should be {expected.full_name})' if expected else ')')),
                      site_id=check.site_id)
    db.session.commit()
    return check


def resolve(check, by):
    check.resolved_at, check.resolved_by = utcnow(), by[:160]
