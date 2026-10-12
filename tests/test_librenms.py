"""Equipment from LibreNMS: matching, adding, placing by site rules, and never overwriting what people typed."""
from conftest import A

DEVICES = [
    {'device_id': 1, 'hostname': '10.16.100.15', 'sysName': 'FCMS-CORE-SW1.fcmiddle.internal', 'ip': '10.16.100.15',
     'hardware': 'ICX8200-24P', 'os': 'ironware', 'version': '10.0.10b', 'serial': 'FMK3240X0AB', 'location': 'MDF',
     'type': 'network', 'status': 1, 'disabled': 0, 'last_polled': '2026-10-12 13:05:00'},
    {'device_id': 2, 'hostname': 'fcms-ap-204', 'sysName': 'fcms-ap-204', 'ip': '10.16.20.44', 'hardware': 'Ruckus R650',
     'os': 'ruckuswireless', 'version': '6.1.2', 'serial': '', 'location': 'Room 204', 'type': 'wireless', 'status': 0,
     'disabled': 0, 'last_polled': '2026-10-12 12:00:00'},
    {'device_id': 3, 'hostname': 'fcms-fw1', 'sysName': 'fcms-fw1', 'ip': '10.16.100.1', 'hardware': 'FortiGate-120G',
     'os': 'fortigate', 'version': 'v7.4.4', 'serial': 'FG120GTK24000001', 'location': 'MDF', 'type': 'network', 'status': 1,
     'disabled': 0},
    {'device_id': 4, 'hostname': 'teacher-pc', 'sysName': 'teacher-pc', 'ip': '10.16.30.9', 'os': 'windows',
     'type': 'workstation', 'status': 1, 'disabled': 0},
    {'device_id': 5, 'hostname': 'old-switch', 'sysName': 'old-switch', 'ip': '10.16.100.99', 'os': 'ironware',
     'type': 'network', 'status': 0, 'disabled': 1},
    {'device_id': 6, 'hostname': 'fchs-ups1', 'sysName': 'fchs-ups1', 'ip': '10.15.1.20', 'hardware': 'Smart-UPS 1500',
     'os': 'apc', 'version': '7.0', 'serial': 'AS1234567890', 'location': 'Server room', 'type': 'power', 'status': 1,
     'disabled': 0},
]


class FakeLibreNMS:
    def __init__(self, devices=DEVICES, status=200):
        self.devices, self.status, self.calls = [dict(d) for d in devices], status, []

    def __call__(self, url, headers=None, timeout=None, verify=None):
        self.calls.append((url, headers))
        payload = {'status': 'ok', 'devices': self.devices} if url.endswith('/devices') else \
            {'status': 'ok', 'system': [{'local_ver': '25.9.0'}]}
        return _Resp(self.status, payload)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def connect(client, monkeypatch, fake=None, **form):
    fake = fake or FakeLibreNMS()
    monkeypatch.setattr(A.requests, 'get', fake)
    data = {'url': 'http://librenms.local:8000/', 'api_token': 'tok-123', 'verify_tls': 'on', 'auto_add': 'on',
            'site_rules': '10.16.0.0/16 = Fox Creek Middle\n10.15. = Fox Creek High'}
    data.update(form)
    client.post('/admin/librenms', data=data)
    return fake


def test_sync_adds_types_places_and_links(client, make, monkeypatch):
    middle, high = make.site('Fox Creek Middle'), make.site('Fox Creek High')
    make.device(tag='FW-01', serial='FG120GTK24000001', device_type='other', site=middle)  # already in FoxDesk
    fake = connect(client, monkeypatch)
    row = A.LibreNMSSettings.query.one()
    assert row.url == 'http://librenms.local:8000' and 'tok-123' not in row.api_token, 'the token is stored encrypted'
    assert b'Connected to LibreNMS 25.9.0: 6 devices' in client.post('/admin/librenms/test', follow_redirects=True).data
    assert fake.calls[0][1] == {'X-Auth-Token': 'tok-123'}

    r = client.post('/admin/librenms/sync', follow_redirects=True)
    assert b'6 devices in LibreNMS, 3 added, 1 matched to devices you had, 0 updated, 2 skipped' in r.data
    switch = A.AssetRegistry.query.filter_by(asset_tag='FCMS-CORE-SW1').one()
    assert (switch.device_type, switch.hostname, switch.ip_address, switch.firmware) == ('switch', 'FCMS-CORE-SW1', '10.16.100.15', '10.0.10b')
    assert switch.device_model.full_name == 'Ruckus ICX8200-24P' and switch.site_id == middle.id
    assert (switch.location.name, switch.location.kind) == ('MDF', 'closet')
    ap = A.AssetRegistry.query.filter_by(asset_tag='fcms-ap-204').one()
    assert ap.device_type == 'access_point' and ap.serial_number is None and ap.location.kind == 'room'
    assert A.monitor_status(ap)['up'] is False
    ups = A.AssetRegistry.query.filter_by(asset_tag='fchs-ups1').one()
    assert (ups.device_type, ups.site_id, ups.device_model.manufacturer) == ('ups', high.id, 'APC')
    fw = A.AssetRegistry.query.filter_by(asset_tag='FW-01').one()
    assert fw.hostname == 'fcms-fw1' and fw.site_id == middle.id and A.AssetRegistry.query.count() == 4, \
        'matched by serial: keeps its own tag and type'
    assert not A.AssetRegistry.query.filter_by(asset_tag='teacher-pc').first(), 'workstations aren\'t equipment'
    assert 'Down' in client.get(f'/admin/assets/{ap.asset_tag}/assign').get_data(as_text=True)


def test_second_sync_refreshes_but_keeps_what_people_typed(client, make, monkeypatch):
    make.site('Fox Creek Middle')
    other = make.site('Annex')
    fake = connect(client, monkeypatch)
    client.post('/admin/librenms/sync')
    switch = A.AssetRegistry.query.filter_by(asset_tag='FCMS-CORE-SW1').one()
    switch.site_id, switch.location_id, switch.funding_source = other.id, None, 'E-rate Category 2'
    A.db.session.commit()
    fake.devices[0]['version'] = '10.0.20'
    r = client.post('/admin/librenms/sync', follow_redirects=True)
    assert b'0 added' in r.data and b'4 updated' in r.data
    switch = A.AssetRegistry.query.filter_by(asset_tag='FCMS-CORE-SW1').one()
    assert switch.firmware == '10.0.20' and switch.site_id == other.id and switch.funding_source == 'E-rate Category 2'


def test_adding_off_and_errors(client, make, monkeypatch):
    connect(client, monkeypatch, auto_add='')
    r = client.post('/admin/librenms/sync', follow_redirects=True)
    assert b'4 not matched (adding is off)' in r.data and A.AssetRegistry.query.count() == 0
    connect(client, monkeypatch, fake=FakeLibreNMS(status=401), api_token='')
    r = client.post('/admin/librenms/sync', follow_redirects=True)
    assert b'rejected the API token' in r.data
    assert 'rejected' in A.LibreNMSSettings.query.one().last_error


def test_site_rules_are_checked(client, make, monkeypatch):
    make.site('Fox Creek Middle')
    r = client.post('/admin/librenms', data={'url': 'http://x', 'site_rules': '10.16.0.0/16 = Nowhere High\nbogus'},
                    follow_redirects=True)
    assert b'Nowhere High' in r.data and b'needs an =' in r.data
    r = client.post('/admin/librenms', data={'url': 'librenms.local'}, follow_redirects=True)
    assert b'starts with http' in r.data
    assert client.post('/admin/sync_schedule/librenms', data={'enabled': 'on', 'interval_hours': '6'}).status_code == 302
    assert A.SyncSchedule.query.filter_by(sync_type='librenms').one().enabled


def test_real_world_quirks(client, make, monkeypatch):
    """What Fox Creek's LibreNMS actually reports: object-id models, Aerohive
    locations that only name the school, school initials, ping-only hosts,
    iDRAC cards, and firewalls on an address no rule covers."""
    middle, high = make.site('Fox Creek Middle School'), make.site('Fox Creek High School')
    devices = [
        {'device_id': 11, 'sysName': 'fcms-mdf-sw1', 'ip': '10.16.100.21', 'hardware': 'registration.68.2.5', 'os': 'ironware',
         'type': 'network', 'status': 1, 'serial': 'FNS1111', 'location': 'FCMS MDF'},
        {'device_id': 12, 'sysName': 'fcms-ap-1', 'ip': '10.16.30.5', 'hardware': 'AP4000-WW', 'os': 'hiveos-wireless',
         'type': 'wireless', 'status': 1, 'serial': 'AH1', 'location': 'Fox Creek Middle School|Fox Creek Middle|FCMS'},
        {'device_id': 13, 'sysName': 'fcms-dns1', 'ip': '10.16.100.113', 'os': 'ping', 'type': '', 'status': 1},
        {'device_id': 14, 'sysName': 'idrac-gv09482', 'ip': '10.16.100.111', 'os': 'drac', 'type': 'management', 'status': 1},
        {'device_id': 15, 'sysName': 'fortigate-400f-fc01', 'ip': '10.255.255.15', 'hardware': 'FortiGate 400F',
         'os': 'fortigate', 'type': 'firewall', 'status': 1, 'location': 'Fox Creek High School'},
    ]
    connect(client, monkeypatch, fake=FakeLibreNMS(devices), site_rules='10.16.0.0/16 = Fox Creek Middle School')
    r = client.post('/admin/librenms/sync', follow_redirects=True)
    assert b'3 added' in r.data and b'2 skipped' in r.data, 'ping-only hosts and iDRAC cards are skipped'
    sw = A.AssetRegistry.query.filter_by(asset_tag='fcms-mdf-sw1').one()
    assert sw.device_model_id is None and sw.description is None, 'an object id isn\'t a model name'
    assert (sw.site_id, sw.location.name, sw.location.kind) == (middle.id, 'MDF', 'closet'), '"FCMS MDF" → MDF at the middle school'
    ap = A.AssetRegistry.query.filter_by(asset_tag='fcms-ap-1').one()
    assert ap.location_id is None and ap.device_model.full_name == 'Extreme Networks AP4000-WW'
    fw = A.AssetRegistry.query.filter_by(asset_tag='fortigate-400f-fc01').one()
    assert fw.site_id == high.id, 'school from the location name when no address rule matches'
