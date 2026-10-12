"""Equipment (servers, network gear...), locations, the E-rate inventory, and support alerts."""
from datetime import date, timedelta

from conftest import A


def add_switch(client, site, location=None, **extra):
    form = {'asset_tag': 'FCMS-CORE-SW1', 'device_type': 'switch', 'site_id': site.id, 'hostname': 'fcms-core-sw1',
            'ip_address': '10.16.100.15', 'firmware': '10.0.10', 'serial_number': 'FMK3240X0AB',
            'location_id': location.id if location else '', 'funding_source': 'E-rate Category 2', 'erate_frn': '2499012345',
            'funding_year': '2025', 'purchase_date': '2025-08-01', 'purchase_cost': '4,850.00',
            'management_url': '10.16.100.15'}
    form.update(extra)
    return client.post('/admin/equipment/new', data=form, follow_redirects=True)


def test_locations_and_equipment(client, make):
    site = make.site('Fox Creek Middle')
    client.post('/admin/locations', data={'name': 'Main building', 'kind': 'building', 'site_id': site.id})
    building = A.Location.query.one()
    client.post('/admin/locations', data={'name': 'MDF', 'kind': 'closet', 'parent_id': building.id})
    mdf = A.Location.query.filter_by(name='MDF').one()
    assert mdf.site_id == site.id and mdf.full_path == 'Fox Creek Middle › Main building › MDF'
    r = client.post('/admin/locations', data={'name': 'mdf', 'kind': 'closet', 'parent_id': building.id}, follow_redirects=True)
    assert b'already there' in r.data

    page = add_switch(client, site, mdf).get_data(as_text=True)
    assert 'Switch <code>FCMS-CORE-SW1</code>' in page and 'Main building › MDF' in page
    assert 'Assign to' not in page and 'Incidents / Damage Reports' not in page, 'equipment isn\'t checked out to people'
    row = A.AssetRegistry.query.filter_by(asset_tag='FCMS-CORE-SW1').one()
    assert row.is_equipment and row.management_url == 'https://10.16.100.15' and float(row.purchase_cost) == 4850.0
    assert A.Asset.query.filter_by(asset_tag='FCMS-CORE-SW1').one().is_valid

    listing = client.get('/admin/equipment?q=10.16.100').get_data(as_text=True)
    assert 'FCMS-CORE-SW1' in listing and 'Switches' in listing
    assert 'FCMS-CORE-SW1' in client.get(f'/admin/equipment?location_id={building.id}').get_data(as_text=True), \
        'a building includes what\'s in its rooms'
    assert 'FMK3240X0AB' in client.get('/admin/equipment/export').get_data(as_text=True)

    r = client.post(f'/admin/locations/{building.id}', data={'action': 'delete'}, follow_redirects=True)
    assert b'still has devices or other locations' in r.data and A.Location.query.count() == 2

    client.post('/admin/equipment/FCMS-CORE-SW1/edit', data={'device_type': 'switch', 'site_id': site.id,
                                                              'location_id': mdf.id, 'firmware': '10.0.20'})
    assert A.AssetRegistry.query.filter_by(asset_tag='FCMS-CORE-SW1').one().firmware == '10.0.20'


def test_location_must_match_school(client, make):
    a, b = make.site('A School'), make.site('B School')
    client.post('/admin/locations', data={'name': 'Room 1', 'kind': 'room', 'site_id': b.id})
    room = A.Location.query.one()
    r = add_switch(client, a, room)
    assert b'different school' in r.data and A.AssetRegistry.query.count() == 0


def test_erate_inventory(client, make):
    site = make.site('Fox Creek High')
    add_switch(client, site)
    add_switch(client, site, asset_tag='OLD-AP', device_type='access_point', serial_number='X1', erate_frn='',
               funding_source='General fund')
    page = client.get('/admin/equipment/erate').get_data(as_text=True)
    assert '2499012345' in page and 'FCMS-CORE-SW1' in page and 'OLD-AP' not in page
    csv_text = client.get('/admin/equipment/erate/export?year=2025').get_data(as_text=True)
    assert 'FMK3240X0AB' in csv_text and 'Fox Creek High' in csv_text


def test_support_status_and_automation(client, make, sent_emails):
    site = make.site()
    add_switch(client, site, support_ends=(date.today() + timedelta(days=60)).isoformat())
    row = A.AssetRegistry.query.one()
    assert A.support_status(row)['css'] == 'badge-yellow'
    assert 'FCMS-CORE-SW1' in client.get('/admin/equipment?support=ending').get_data(as_text=True)
    rule = A.AutomationRule(name='Renewals', trigger='equipment.support_ending', enabled=True,
                            conditions=[{'field': 'device.support_days_left', 'op': 'lte', 'value': '90'}],
                            actions=[{'type': 'send_email', 'params': {'to': 'address', 'address': 'it@district.org',
                                                                       'subject': 'Support ends {support_ends}: {asset_tag}',
                                                                       'body': 'x'}}])
    A.db.session.add(rule)
    A.db.session.commit()
    assert A.run_scheduled() == 1 and A.run_scheduled() == 0
    assert sent_emails[-1][0] == 'it@district.org' and 'FCMS-CORE-SW1' in sent_emails[-1][1]
    assert 'equipment_support_email' in A.TEMPLATES_BY_KEY


def test_type_labels(client, make):
    make.device(device_type='access_point')
    assert 'Access points' in client.get('/admin').get_data(as_text=True)
    assert A.type_label('ups', plural=True) == 'UPSes'
