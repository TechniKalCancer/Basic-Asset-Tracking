"""Shared vendor detection for mixed-device districts."""
from conftest import A
from foxdesk.integrations.vendors import (
    normalize_device_identity, normalize_vendor_name, supported_vendors, vendor_from_serial, vendor_name,
)


def test_maker_names_as_they_appear_in_inventory_data():
    cases = {
        'Dell Inc.': 'dell', 'HP Inc.': 'hp', 'Hewlett-Packard': 'hp', 'LENOVO': 'lenovo', 'Apple Inc.': 'apple',
        'ASUSTeK COMPUTER INC.': 'asus', 'Acer America Corp.': 'acer', 'Microsoft Corporation': 'microsoft',
        'Hewlett Packard Enterprise': 'hpe', 'Aruba, a Hewlett Packard Enterprise company': 'hpe',
        'Brocade Communications': 'ruckus', 'KYOCERA Document Solutions Inc.': 'kyocera', 'Fortinet, Inc.': 'fortinet',
        'Ubiquiti Inc.': 'ubiquiti', 'American Power Conversion': 'apc', 'Samsung Techwin': 'hanwha',
        'Acme Widgets': None, '': None, None: None,
    }
    for raw, key in cases.items():
        assert normalize_vendor_name(raw) == key, raw


def test_model_strings_from_google_and_product_lines():
    for model, key in [('Dell Chromebook 3120', 'dell'), ('ASUS Chromebook C202XA / C203XA', 'asus'),
                       ('Lenovo 100e Chromebook 2nd Gen MTK', 'lenovo'), ('Promethean Chromebox 2', 'promethean'),
                       ('AOpen Chromebox Mini', 'acer'), ('ThinkPad T14 Gen 3', 'lenovo'), ('OptiPlex 7090', 'dell'),
                       ('Surface Pro 7', 'microsoft'), ('SMART Board 7086', 'smart'), ('Smart TV in the library', None)]:
        assert normalize_vendor_name(model) == key, model


def test_named_maker_wins_and_unknown_makers_are_not_guessed():
    assert vendor_from_serial('ABC1234', manufacturer='HP Inc.') == 'hp'
    assert vendor_from_serial(None, manufacturer='Dell Inc.') == 'dell', 'a named maker needs no serial'
    assert vendor_from_serial('C02ABC12EF', manufacturer='Apple') == 'apple'
    assert vendor_from_serial('ABC1234', manufacturer='Acme Widgets') is None, 'named but unknown: no serial guess'
    assert vendor_from_serial('NXAB12C', model='Acer Chromebook 311') == 'acer'


def test_serial_shape_is_only_a_last_resort():
    assert vendor_from_serial('5PQ2X73') == 'dell', '7-character service tag'
    assert vendor_from_serial('CND1234567') is None, '10 characters says nothing about the maker'
    assert vendor_from_serial('FCHS106A02') is None
    assert vendor_from_serial('ABC1234', description='Brittany Smith') == 'dell', 'a person\'s name names no maker'


def test_device_identity_and_categories():
    named = normalize_device_identity('CND1234567', manufacturer='HP')
    assert (named['vendor'], named['vendor_label'], named['how']) == ('hp', 'HP', 'named')
    guessed = normalize_device_identity('5PQ2X73')
    assert (guessed['vendor'], guessed['how']) == ('dell', 'serial')
    assert normalize_device_identity('???')['how'] is None
    assert {'ruckus', 'fortinet', 'cisco'} <= set(supported_vendors('network'))
    assert 'kyocera' in supported_vendors('printers') and 'promethean' in supported_vendors('av')
    assert vendor_name('dell inc.') == 'Dell' and vendor_name('aruba networks') == 'HPE' and vendor_name('Foo') is None


def test_device_model_form_tidies_known_makers(client):
    client.post('/admin/device_models/new', data={'manufacturer': 'DELL INC.', 'model_name': 'Chromebook 3120',
                                                  'device_type': 'chromebook'})
    assert A.DeviceModel.query.one().manufacturer == 'Dell'
    r = client.post('/admin/device_models/new', data={'manufacturer': 'Dell', 'model_name': 'Chromebook 3120',
                                                      'device_type': 'chromebook'}, follow_redirects=True)
    assert b'already exists' in r.data, '"DELL INC." and "Dell" are the same maker'
    client.post('/admin/device_models/new', data={'manufacturer': 'Acme Widgets', 'model_name': 'W1', 'device_type': 'other'})
    assert A.DeviceModel.query.filter_by(model_name='W1').one().manufacturer == 'Acme Widgets', 'unknown makers are kept as typed'
    assert 'list="vendorNames"' in client.get('/admin/device_models/new').get_data(as_text=True)
