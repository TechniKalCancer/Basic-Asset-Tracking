"""Parts inventory: stock always matches its history, using parts on repairs/tickets, low stock."""
from decimal import Decimal

from flask import g

from conftest import A


def model(name='Chromebook 3100'):
    m = A.DeviceModel(manufacturer='Dell', model_name=name, device_type='chromebook')
    A.db.session.add(m)
    A.db.session.commit()
    return m


def repair_on(make, device_model):
    dev = make.device()
    A.AssetRegistry.query.get(dev.id).device_model_id = device_model.id
    cat = make.ticket_category('Device Repair')
    t = A.Ticket(category_id=cat.id, subject='Repair', description='d', asset_tag=dev.asset_tag)
    A.db.session.add(t)
    A.db.session.flush()
    r = A.Repair(asset_tag=dev.asset_tag, issue_description='Cracked screen', ticket_id=t.id)
    A.db.session.add(r)
    A.db.session.commit()
    return r


def test_stock_changes_add_up(client):
    m = model()
    r = client.post('/admin/parts/new', data={'name': '11.6" screen', 'category': 'screen', 'fits_models': [str(m.id)],
                                              'quantity': '5', 'reorder_level': '2', 'unit_cost': '$40'})
    part = A.Part.query.one()
    assert r.status_code == 302 and part.quantity_on_hand == 5 and part.fits_models == [m.id] and part.unit_cost == Decimal('40.00')
    client.post(f'/admin/parts/{part.id}/stock', data={'action': 'received', 'quantity': '3', 'note': 'PO 1234'})
    client.post(f'/admin/parts/{part.id}/stock', data={'action': 'count', 'counted': '7', 'note': 'one cracked in the box'})
    part = A.Part.query.one()
    moves = A.PartMovement.query.filter_by(part_id=part.id).order_by(A.PartMovement.id).all()
    assert [(m.reason, m.change, m.quantity_after) for m in moves] == [('received', 5, 5), ('received', 3, 8), ('adjusted', -1, 7)]
    assert part.quantity_on_hand == sum(m.change for m in moves)
    body = client.get(f'/admin/parts/{part.id}').get_data(as_text=True)
    assert 'PO 1234' in body and 'Count corrected' in body


def test_use_on_repair_with_charge(client, make):
    m = model()
    other = model('Latitude 3120')
    screen = A.Part(name='11.6" screen', category='screen', fits_models=[m.id], quantity_on_hand=2, reorder_level=1,
                    unit_cost=Decimal('40.00'))
    charger = A.Part(name='45W charger', category='charger', quantity_on_hand=4)
    keyboard = A.Part(name='Latitude keyboard', category='keyboard', fits_models=[other.id], quantity_on_hand=3)
    A.db.session.add_all([screen, charger, keyboard])
    A.db.session.commit()
    repair = repair_on(make, m)

    body = client.get(f'/admin/repairs/{repair.id}').get_data(as_text=True)
    assert 'Parts used' in body and 'fits this model' in body and 'Latitude keyboard' not in body, \
        'parts for other models are left out; ones for this model come first'
    assert body.index('11.6&#34; screen') < body.index('45W charger')

    r = client.post('/admin/parts/use', data={'part_id': screen.id, 'quantity': '1', 'repair_id': repair.id, 'charge': 'on'},
                    follow_redirects=True)
    assert b'Running low' in r.data
    screen = A.Part.query.get(screen.id)
    move = A.PartMovement.query.filter_by(part_id=screen.id, reason='used').one()
    assert screen.quantity_on_hand == 1 and screen.is_low
    assert (move.repair_id, move.ticket_id, move.asset_tag) == (repair.id, repair.ticket_id, repair.asset_tag)
    charge = A.TicketCharge.query.filter_by(ticket_id=repair.ticket_id).one()
    assert charge.amount == Decimal('40.00') and '11.6' in charge.description

    r = client.post('/admin/parts/use', data={'part_id': screen.id, 'quantity': '5', 'repair_id': repair.id},
                    follow_redirects=True)
    assert b'Only 1' in r.data and A.Part.query.get(screen.id).quantity_on_hand == 1, 'stock never goes negative'
    assert '11.6' in client.get(f'/admin/tickets/{repair.ticket_id}').get_data(as_text=True), 'shows on the ticket too'


def test_low_stock_list_and_badge(client):
    A.db.session.add_all([A.Part(name='Battery', category='battery', quantity_on_hand=1, reorder_level=3),
                          A.Part(name='Spare hinge kit', category='hinge', quantity_on_hand=9, reorder_level=3),
                          A.Part(name='Webcam module', category='camera', quantity_on_hand=0, reorder_level=0)])
    A.db.session.commit()
    body = client.get('/admin/parts?show=low').get_data(as_text=True)
    assert 'Battery' in body and 'Spare hinge kit' not in body and 'Webcam module' not in body, 'reorder level 0 never flags'
    assert '1 running low' in client.get('/admin/parts').get_data(as_text=True)
    assert 'nav-tab-badge">1<' in client.get('/admin/repairs').get_data(as_text=True)


def test_parts_can_be_switched_off(client):
    A.set_feature('parts', False, 'test')
    A.db.session.commit()
    g.pop('_feature_overrides', None)
    assert client.get('/admin/parts').status_code == 404
