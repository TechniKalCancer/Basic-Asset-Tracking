"""End-of-year turn-in: devices scanned in go into numbered boxes, box_size
to a box, in scan order, so each can go back to the same student in the
fall ("hand back"). The assignment normally stays over the summer; a
session with returns_device (a graduating class) unassigns at turn-in.

Who hasn't turned in yet = devices assigned at the session's school (end-user
devices only) that haven't been scanned into the session.
"""
from collections import OrderedDict

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import Asset, AssetRegistry, CollectionScan, END_USER_TYPES, Person
from foxdesk.services.util import resolve_scan


def _registry(session, tag):
    query = AssetRegistry.query.filter_by(asset_tag=tag)
    if session.site_id:
        query = query.filter(AssetRegistry.site_id == session.site_id)
    return query.first()


def next_slot(session):
    n = CollectionScan.query.filter_by(session_id=session.id).count()
    size = max(session.box_size or 10, 1)
    return n // size + 1, n % size + 1


def turn_in(session, scan_value, actor, note=None):
    """(status, message, scan) — status 'ok', 'already' or 'error'. Does not commit."""
    from foxdesk.services.assignments import _close_open_assignment
    if session.closed_at:
        return 'error', 'This turn-in is closed. Reopen it to keep scanning.', None
    tag, _ = resolve_scan((scan_value or '').strip())
    row = _registry(session, tag) if tag else None
    if row is None:
        where = f' at {session.site.name}' if session.site else ''
        return 'error', f'"{scan_value}" isn\'t a device{where}.', None
    existing = CollectionScan.query.filter_by(session_id=session.id, asset_tag=row.asset_tag).first()
    if existing:
        return 'already', f'{row.asset_tag} is already in box {existing.box}, slot {existing.slot}.', existing
    asset = Asset.query.filter_by(asset_tag=row.asset_tag).first()
    person = asset.assigned_to if asset else None
    box, slot = next_slot(session)
    scan = CollectionScan(session_id=session.id, asset_tag=row.asset_tag, person_id=person.id if person else None,
                          person_name=person.full_name if person else None, box=box, slot=slot,
                          note=(note or '').strip()[:255] or None, scanned_by=(actor or '')[:160] or None, scanned_at=utcnow())
    db.session.add(scan)
    if session.returns_device and asset and asset.assigned_to_id:
        _close_open_assignment(row.asset_tag, condition_in=f'Turned in ({session.name})')
        asset.assigned_to_id, asset.status = None, 'available'
    who = person.full_name if person else 'not assigned to anyone'
    return 'ok', f'{row.asset_tag} ({who}) → box {box}, slot {slot}.', scan


def hand_back(session, scan_value, actor):
    """Mark a boxed device as given back. (status, message, scan)."""
    tag, _ = resolve_scan((scan_value or '').strip())
    scan = CollectionScan.query.filter_by(session_id=session.id, asset_tag=tag).first() if tag else None
    if scan is None:
        return 'error', f'"{scan_value}" wasn\'t turned in during {session.name}.', None
    if scan.handed_back_at:
        return 'already', f'{scan.asset_tag} already went back to {scan.person_name or "someone"} on {scan.handed_back_at:%b %d}.', scan
    scan.handed_back_at, scan.handed_back_by = utcnow(), (actor or '')[:160] or None
    return 'ok', f'{scan.asset_tag} back to {scan.person_name or "(no one)"} — it was in box {scan.box}, slot {scan.slot}.', scan


def outstanding(session):
    """[(registry row, person)] assigned at the session's school, not turned in."""
    scanned = {t for (t,) in CollectionScan.query.filter_by(session_id=session.id).with_entities(CollectionScan.asset_tag)}
    query = (db.session.query(AssetRegistry, Person).join(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
             .join(Person, Person.id == Asset.assigned_to_id)
             .filter(AssetRegistry.device_type.in_(END_USER_TYPES), AssetRegistry.is_loaner.isnot(True)))
    if session.site_id:
        query = query.filter(AssetRegistry.site_id == session.site_id)
    return [(r, p) for r, p in query.order_by(Person.last_name, Person.first_name) if r.asset_tag not in scanned]


def boxes(session):
    """OrderedDict box -> scans by slot."""
    out = OrderedDict()
    for s in CollectionScan.query.filter_by(session_id=session.id).order_by(CollectionScan.box, CollectionScan.slot):
        out.setdefault(s.box, []).append(s)
    return out


def find(session, q):
    """Scans whose student name or tag matches q (where's Ana's device?)."""
    q = (q or '').strip()
    if len(q) < 2:
        return []
    like = f'%{q}%'
    return (CollectionScan.query.filter(CollectionScan.session_id == session.id,
                                        db.or_(CollectionScan.person_name.ilike(like), CollectionScan.asset_tag.ilike(like)))
            .order_by(CollectionScan.box, CollectionScan.slot).limit(25).all())
