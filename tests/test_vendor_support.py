"""Shared vendor detection for mixed-device districts."""

from foxdesk.integrations.vendors import normalize_device_identity, vendor_from_serial


def test_explicit_manufacturer_overrides_serial_shape():
    assert vendor_from_serial('ABC1234', manufacturer='HP Inc.') == 'hp'
    assert vendor_from_serial('ABC1234', manufacturer='Lenovo') == 'lenovo'
    assert vendor_from_serial('ABC1234', manufacturer='Dell Inc.') == 'dell'


def test_serial_shape_is_a_safe_fallback_when_vendor_is_unknown():
    assert vendor_from_serial('ABC1234') == 'dell'
    assert vendor_from_serial('CND1234567') == 'hp'
    assert vendor_from_serial('PF2W74JQ', manufacturer='ThinkPad') == 'lenovo'


def test_device_identity_normalization_tracks_vendor_and_serial():
    result = normalize_device_identity('CND1234567', manufacturer='HP')
    assert result['vendor'] == 'hp'
    assert result['vendor_label'] == 'HP'
    assert result['serial_number'] == 'CND1234567'
