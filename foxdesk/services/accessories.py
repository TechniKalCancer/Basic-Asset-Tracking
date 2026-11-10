"""Accessories that go out with a device (charger, case, stylus) and come back
with it, or don't. Each assignment remembers which went out
(AssignmentAccessory); when the device comes back, a missing one can become a
damage fee (an Incident, same as any other) for the person who had it."""
from decimal import Decimal

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import AccessoryType, AssignmentAccessory, AssignmentHistory


def types_for(device_type):
    """Active accessory types for this kind of device (or for every kind)."""
    return (AccessoryType.query.filter(AccessoryType.is_active.is_(True),
                                       db.or_(AccessoryType.device_type.is_(None), AccessoryType.device_type == '',
                                              AccessoryType.device_type == device_type))
            .order_by(AccessoryType.sort_order, AccessoryType.name).all())


def open_assignment(asset_tag):
    return AssignmentHistory.query.filter_by(asset_tag=asset_tag, unassigned_at=None) \
        .order_by(AssignmentHistory.assigned_at.desc()).first()


def issue(assignment, type_ids):
    """Record which accessories went out with this assignment. Does not commit."""
    wanted = {int(t) for t in type_ids if str(t).isdigit()}
    if not assignment or not wanted:
        return []
    out = []
    for t in AccessoryType.query.filter(AccessoryType.id.in_(wanted)):
        row = AssignmentAccessory(assignment_id=assignment.id, accessory_type_id=t.id, name=t.name, fee=t.fee)
        db.session.add(row)
        out.append(row)
    return out


def outstanding(assignment):
    """Accessories that went out with this assignment and haven't been settled."""
    if not assignment:
        return []
    return [a for a in assignment.accessories if not a.returned_at and not a.missing_at]


def settle(assignment, came_back_ids, charge_ids, person, asset_tag):
    """At turn-in: mark the ticked accessories returned and the rest missing,
    charging the missing ones whose box was ticked as a damage fee. Does not
    commit. Returns (returned names, missing names, incidents made)."""
    from foxdesk.services.incidents import _create_incident
    came_back = {int(i) for i in came_back_ids if str(i).isdigit()}
    charge = {int(i) for i in charge_ids if str(i).isdigit()}
    now, returned, missing, incidents = utcnow(), [], [], []
    for a in outstanding(assignment):
        if a.id in came_back:
            a.returned_at = now
            returned.append(a.name)
            continue
        a.missing_at = now
        missing.append(a.name)
        if a.id in charge and a.fee:
            incident = _create_incident(asset_tag, person, f'{a.name} not returned with the device',
                                        fee_charged=True, fee_amount=Decimal(a.fee))
            db.session.flush()
            a.incident_id = incident.id
            incidents.append(incident)
    return returned, missing, incidents


def held_by(person_id):
    """{asset_tag: [accessory names]} for the person's devices out right now."""
    rows = (db.session.query(AssignmentHistory.asset_tag, AssignmentAccessory.name)
            .join(AssignmentAccessory, AssignmentAccessory.assignment_id == AssignmentHistory.id)
            .filter(AssignmentHistory.person_id == person_id, AssignmentHistory.unassigned_at.is_(None),
                    AssignmentAccessory.returned_at.is_(None), AssignmentAccessory.missing_at.is_(None)))
    out = {}
    for tag, name in rows:
        out.setdefault(tag, []).append(name)
    return out
