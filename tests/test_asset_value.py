"""Device value (straight-line over a useful life) and repair cost by model."""
from datetime import date, timedelta
from decimal import Decimal

from conftest import A
from foxdesk.services.asset_value import book_value, months_between


def test_book_value_math():
    assert months_between(date(2024, 8, 15), date(2026, 8, 14)) == 23
    assert book_value(Decimal('240'), date(2024, 8, 1), 4, today=date(2026, 8, 1)) == Decimal('120.00'), 'half the life gone'
    assert book_value(240, date(2020, 1, 1), 4, today=date(2026, 1, 1)) == Decimal('0.00'), 'never below zero'
    assert book_value(240, date(2020, 1, 1), 4, salvage_percent=10, today=date(2026, 1, 1)) == Decimal('24.00')
    assert book_value(None, date(2024, 1, 1), 4) is None and book_value(100, None, 4) is None


def test_value_page_fill_and_report(client, make):
    site = make.site('Middle')
    rows = [make.device(site=site, google_model='Dell Chromebook 3120') for _ in range(3)]
    make.device(site=site, device_type='desktop')
    page = client.get('/admin/value').get_data(as_text=True)
    assert '0 of 4' in page and 'Fill in purchase info' in page
    two_years_ago = (date.today().replace(day=1) - timedelta(days=730)).replace(day=1).isoformat()
    r = client.post('/admin/value/fill', data={'model': 'Dell Chromebook 3120', 'purchase_date': two_years_ago,
                                               'purchase_cost': '240', 'funding_source': 'ESSER III'}, follow_redirects=True)
    assert b'Updated 3 devices.' in r.data
    row = A.db.session.get(A.AssetRegistry, rows[0].id)
    assert (row.purchase_cost, row.funding_source) == (Decimal('240.00'), 'ESSER III')
    again = client.post('/admin/value/fill', data={'model': 'Dell Chromebook 3120', 'purchase_cost': '999'}, follow_redirects=True)
    assert b'Updated 0 devices.' in again.data, 'blanks only unless told to replace'
    report = client.get('/admin/value?group=funding').get_data(as_text=True)
    assert 'ESSER III' in report and '$720.00' in report and '3 of 4' in report
    assert 'ESSER III' in client.get('/admin/value?group=funding&export=1').get_data(as_text=True)
    assert 'Worth $' in client.get(f'/admin/assets/{rows[0].asset_tag}/assign').get_data(as_text=True)
    client.post('/admin/value/settings', data={'life_chromebook': '5', 'salvage_percent': '10', 'labor_rate': '30'})
    from foxdesk.services.asset_value import life_for, value_settings
    assert life_for('chromebook') == 5 and value_settings().salvage_percent == 10
    assert b'Pick which devices' in client.post('/admin/value/fill', data={'purchase_cost': '1'}, follow_redirects=True).data


def test_repair_cost_by_model(client, make):
    client.post('/admin/value/settings', data={'labor_rate': '30'})
    devices = [make.device(google_model='Dell Chromebook 3120') for _ in range(4)] + [make.device(google_model='HP 11 G9')]
    for row in devices[:2]:
        client.post(f'/admin/assets/{row.asset_tag}/repairs/send', data={'issue_description': 'Screen'})
    repair = A.Repair.query.filter_by(asset_tag=devices[0].asset_tag).one()
    client.post(f'/admin/repairs/{repair.id}/update', data={'minutes': '30', 'note': 'Swapped LCD'})
    client.post(f'/admin/repairs/{repair.id}/update', data={'minutes': '30'})
    assert A.db.session.get(A.Repair, repair.id).labor_minutes == 60
    part = A.Part(name='LCD panel', quantity_on_hand=5, unit_cost=Decimal('45'))
    A.db.session.add(part)
    A.db.session.flush()
    A.db.session.add(A.PartMovement(part_id=part.id, change=-1, reason='used', quantity_after=4, repair_id=repair.id))
    A.db.session.add(A.TicketCharge(ticket_id=repair.ticket_id, description='Screen', amount=Decimal('60')))
    A.db.session.commit()
    page = client.get('/admin/repairs/costs').get_data(as_text=True)
    assert 'Dell Chromebook 3120' in page and 'HP 11 G9' not in page, 'only models with repairs'
    from foxdesk.services.asset_value import repair_costs
    row = repair_costs()[0]
    assert (row['devices'], row['repairs'], row['per100'], row['parts'], row['hours'], row['labor'], row['billed']) == \
        (4, 2, 50.0, Decimal('45'), 1.0, Decimal('30.00'), Decimal('60'))
    assert 'Dell Chromebook 3120' in client.get('/admin/repairs/costs?period=all&export=1').get_data(as_text=True)
