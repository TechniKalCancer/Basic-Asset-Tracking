"""Dell warranty lookup, with Dell's API faked."""
from datetime import date, datetime, timedelta

from conftest import A, patch_everywhere


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def dell_item(tag, end, ship='2023-03-12T05:00:00Z', invalid=False, level='ProSupport'):
    return {'serviceTag': tag, 'invalid': invalid, 'productLineDescription': 'CHROMEBOOK 3110',
            'shipDate': None if invalid else ship,
            'entitlements': [] if invalid else [
                {'startDate': '2023-03-12T05:00:00Z', 'endDate': '2024-03-12T04:59:59.999Z', 'serviceLevelDescription': 'Basic'},
                {'startDate': '2024-03-12T05:00:00Z', 'endDate': end, 'serviceLevelDescription': level}]}


def fake_dell(monkeypatch, items_by_tag, token_status=200):
    calls = []

    def token(client_id, secret):
        return FakeResponse(token_status, {'access_token': 'tok'} if token_status == 200 else {})

    def entitlements(tok, tags):
        calls.append(list(tags))
        return FakeResponse(200, [items_by_tag[t] for t in tags if t in items_by_tag])
    patch_everywhere(monkeypatch, '_post_token', token)
    patch_everywhere(monkeypatch, '_get_entitlements', entitlements)
    return calls


def keyed(client):
    client.post('/admin/warranty/credentials', data={'client_id': 'l7xx-test', 'client_secret': 'shh-not-real'})


def test_lookup_fills_warranty_and_ship_dates(client, make, monkeypatch):
    keyed(client)
    fresh = make.device(serial='ABC1234')
    extended = make.device(serial='DEF5678')
    A.AssetRegistry.query.get(extended.id).warranty_expiration = date(2030, 1, 1)   # bought an extension elsewhere
    A.AssetRegistry.query.get(extended.id).purchase_date = date(2023, 1, 1)
    make.device(serial='PF3XY7Z')                                                   # 7 chars, not Dell
    make.device(serial='C02XL0ABCD12')                                              # not a service tag at all
    A.db.session.commit()
    calls = fake_dell(monkeypatch, {'ABC1234': dell_item('ABC1234', '2027-03-12T04:59:59.999Z'),
                                    'DEF5678': dell_item('DEF5678', '2026-03-12T04:59:59.999Z'),
                                    'PF3XY7Z': dell_item('PF3XY7Z', None, invalid=True)})
    r = client.post('/admin/warranty/run', follow_redirects=True)
    assert b'3 checked: 1 warranty dates updated, 1 already current, 1 not Dell' in r.data
    assert calls == [['ABC1234', 'DEF5678', 'PF3XY7Z']] and 'C02XL0ABCD12' not in calls[0]
    row = A.AssetRegistry.query.get(fresh.id)
    assert row.warranty_expiration == date(2027, 3, 12) and row.purchase_date == date(2023, 3, 12)
    ext = A.AssetRegistry.query.get(extended.id)
    assert ext.warranty_expiration == date(2030, 1, 1) and ext.purchase_date == date(2023, 1, 1), 'later/manual dates kept'
    assert A.DeviceRecord.query.filter_by(source='dell', external_key='PF3XY7Z').one().raw == {'not_dell': True}
    page = client.get(f'/admin/assets/{fresh.asset_tag}/assign').get_data(as_text=True)
    assert 'warranty to 2027-03-12' in page and 'ProSupport' in page

    assert client.post('/admin/warranty/run', follow_redirects=True) and calls[1:] == [], 'not asked again for 30 days'
    for rec in A.DeviceRecord.query.filter_by(source='dell'):
        rec.last_synced_at = datetime.utcnow() - timedelta(days=45)
    A.db.session.commit()
    assert {r.serial_number for r in A.candidates()} == {'ABC1234', 'DEF5678'}, 'not-Dell tags wait 180 days'


def test_batches_of_100(client, make, monkeypatch):
    keyed(client)
    for i in range(150):
        make.device(serial=f'Z{i:06d}')
    calls = fake_dell(monkeypatch, {})
    summary = A.run_dell_lookup()
    assert [len(c) for c in calls] == [100, 50] and summary['missing'] == 150 and summary['remaining'] == 0


def test_credentials_and_errors(client, make, monkeypatch):
    assert not A.feature_enabled('dell_warranty')
    assert b'Not configured' in client.get('/admin/warranty').data
    keyed(client)
    row = A.WarrantySettings.query.get(1)
    assert row.dell_client_id == 'l7xx-test' and 'shh-not-real' not in row.dell_client_secret
    assert A.dell_credentials()[1] == 'shh-not-real' and A.feature_enabled('dell_warranty')
    body = client.get('/admin/warranty').get_data(as_text=True)
    assert 'shh-not-real' not in body and 'Saved. Leave blank to keep it' in body

    make.device(serial='ABC1234')
    fake_dell(monkeypatch, {}, token_status=401)
    r = client.post('/admin/warranty/test', follow_redirects=True)
    assert b'Dell rejected the API key' in r.data
    r = client.post('/admin/warranty/run', follow_redirects=True)
    assert b'Dell rejected the API key' in r.data and 'rejected' in A.WarrantySettings.query.get(1).last_error


def test_scheduled_lookup(client, make, monkeypatch):
    keyed(client)
    make.device(serial='ABC1234')
    fake_dell(monkeypatch, {'ABC1234': dell_item('ABC1234', '2027-03-12T04:59:59.999Z')})
    schedule = A._get_or_create_sync_schedule('dell')
    schedule.enabled = True
    A.db.session.commit()
    A._run_due_scheduled_syncs()
    assert A.SyncSchedule.query.filter_by(sync_type='dell').one().last_run_summary.startswith('1 checked: 1 warranty')
