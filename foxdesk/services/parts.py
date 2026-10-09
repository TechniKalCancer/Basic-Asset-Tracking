"""Parts inventory: every stock change goes through record_movement."""
from decimal import Decimal

from foxdesk.core import db
from foxdesk.models import DeviceModel, Part, PartMovement, TicketCharge
from foxdesk.services.auth import _current_actor, _log_activity


def record_movement(part, change, reason, note=None, repair=None, ticket=None, asset_tag=None):
    """Change a part's stock by `change` and record why. Raises ValueError
    if it would go below zero. Does not commit."""
    change = int(change)
    if change == 0:
        raise ValueError('Enter a quantity other than zero.')
    if part.quantity_on_hand + change < 0:
        raise ValueError(f'Only {part.quantity_on_hand} {part.name} on hand.')
    part.quantity_on_hand += change
    move = PartMovement(part_id=part.id, change=change, reason=reason, quantity_after=part.quantity_on_hand,
                        repair_id=repair.id if repair else None,
                        ticket_id=ticket.id if ticket else (repair.ticket_id if repair else None),
                        asset_tag=asset_tag or (repair.asset_tag if repair else (ticket.asset_tag if ticket else None)),
                        note=(note or '').strip()[:255] or None, actor_label=_current_actor()[1])
    db.session.add(move)
    verb = {'received': 'Received', 'used': 'Used', 'returned': 'Returned'}.get(reason, 'Corrected count of')
    where = f' on {move.asset_tag}' if reason == 'used' and move.asset_tag else ''
    _log_activity('part_stock', f'{verb} {abs(change)} × {part.name}{where} (now {part.quantity_on_hand}).',
                  site_id=part.site_id, ticket_id=move.ticket_id)
    return move


def use_part(part, quantity, repair=None, ticket=None, charge=False):
    """Take parts out of stock for a repair or ticket; with charge=True also
    add the cost to the ticket (the repair's ticket, for a repair). Returns
    the movement. Does not commit."""
    quantity = int(quantity)
    if quantity < 1:
        raise ValueError('Use at least one.')
    move = record_movement(part, -quantity, 'used', repair=repair, ticket=ticket)
    if charge:
        ticket_id = ticket.id if ticket else (repair.ticket_id if repair else None)
        if not ticket_id:
            raise ValueError('There\'s no ticket to charge.')
        if part.unit_cost is None:
            raise ValueError(f'{part.name} has no unit cost set.')
        db.session.add(TicketCharge(ticket_id=ticket_id, description=f'{part.name}' + (f' × {quantity}' if quantity > 1 else ''),
                                    amount=(Decimal(part.unit_cost) * quantity).quantize(Decimal('0.01'))))
    return move


def parts_for_model(device_model_id):
    """Active parts that fit this model first, then the model-agnostic ones."""
    parts = Part.query.filter_by(is_active=True).order_by(Part.category, Part.name).all()
    fits = [p for p in parts if device_model_id and device_model_id in (p.fits_models or [])]
    generic = [p for p in parts if not p.fits_models]
    return fits + generic


def low_stock_query():
    return Part.query.filter(Part.is_active.is_(True), Part.reorder_level > 0,
                             Part.quantity_on_hand <= Part.reorder_level)


def model_names(ids):
    if not ids:
        return []
    return [m.full_name for m in DeviceModel.query.filter(DeviceModel.id.in_(ids)).order_by(DeviceModel.manufacturer, DeviceModel.model_name)]
