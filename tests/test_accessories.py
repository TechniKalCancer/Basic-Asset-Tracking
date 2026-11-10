"""Accessories that go out with a device, and charging for ones that don't come back."""
from decimal import Decimal

from conftest import A


def setup_types(client):
    client.post('/admin/accessories', data={'name': 'Charger', 'device_type': 'chromebook', 'fee': '25', 'default_on': 'on'})
    client.post('/admin/accessories', data={'name': 'Case', 'device_type': 'chromebook', 'fee': '15'})
    client.post('/admin/accessories', data={'name': 'Hotspot cable', 'device_type': 'hotspot', 'fee': '5', 'default_on': 'on'})
    return {t.name: t for t in A.AccessoryType.query.all()}


def test_accessories_go_out_and_get_charged_when_missing(client, make):
    types = setup_types(client)
    ana = make.person('Ana', 'Ruiz')
    row = make.device()
    page = client.get(f'/admin/assets/{row.asset_tag}/assign').get_data(as_text=True)
    going_out = page[page.index('Going out with it'):page.index('</fieldset>', page.index('Going out with it'))]
    assert 'Charger' in going_out and 'Hotspot cable' not in going_out, 'only this kind of device'

    r = client.post(f'/admin/assets/{row.asset_tag}/assign', data={'person_id': ana.id,
                                                                   'accessory_id': [types['Charger'].id, types['Case'].id]},
                    follow_redirects=True)
    assert b'With: Case, Charger.' in r.data or b'With: Charger, Case.' in r.data
    assert 'With: ' in client.get(f'/admin/people/{ana.id}').get_data(as_text=True)

    page = client.get(f'/admin/assets/{row.asset_tag}/assign').get_data(as_text=True)
    assert 'Came back with it?' in page and 'charge Ana $25.00' in page
    out = {a.name: a for a in A.AssignmentAccessory.query.all()}
    r = client.post(f'/admin/assets/{row.asset_tag}/unassign', data={'accessory_back': [out['Case'].id],
                                                                     'accessory_charge': [out['Charger'].id, out['Case'].id]},
                    follow_redirects=True)
    assert b'Not returned: Charger; charged $25.00 to Ana Ruiz.' in r.data
    charger = A.db.session.get(A.AssignmentAccessory, out['Charger'].id)
    assert charger.missing_at and charger.incident_id and A.db.session.get(A.AssignmentAccessory, out['Case'].id).returned_at
    incident = A.db.session.get(A.Incident, charger.incident_id)
    assert (incident.person_id, incident.fee_amount, incident.fee_charged) == (ana.id, Decimal('25.00'), True)
    assert 'Charger not returned' in incident.description


def test_bulk_assign_uses_defaults_and_types_can_retire(client, make):
    types = setup_types(client)
    client.post(f'/admin/accessories/{types["Case"].id}', data={'action': 'toggle'})
    assert not A.db.session.get(A.AccessoryType, types['Case'].id).is_active
    row = make.device()
    from foxdesk.services import accessories
    assert [t.name for t in accessories.types_for('chromebook')] == ['Charger'], 'retired ones stop showing'
    ana = make.person('Ana', 'Ruiz')
    A._assign_asset_to_person(row.asset_tag, ana)
    accessories.issue(accessories.open_assignment(row.asset_tag), [t.id for t in accessories.types_for('chromebook') if t.default_on])
    A.db.session.commit()
    assert [a.name for a in A.AssignmentAccessory.query.all()] == ['Charger']
    assert b'Give the accessory a name' in client.post('/admin/accessories', data={'name': ''}, follow_redirects=True).data
