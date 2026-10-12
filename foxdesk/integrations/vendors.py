"""Shared vendor detection for district device lifecycles.

This module is intentionally lightweight and conservative: it adds a single
normalization layer for the hardware vendors the app expects to support in the
field, mainly Dell, Lenovo, and HP. The goal is not to replace vendor APIs;
it is to make every future integration speak the same vocabulary so the rest of
FoxDesk can treat device records consistently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


SUPPORTED_VENDOR_KEYS = ('dell', 'lenovo', 'hp')


@dataclass(frozen=True)
class VendorProfile:
    """Vendor-specific metadata and the heuristics used to recognize it."""
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    serial_patterns: tuple[str, ...] = ()
    notes: str = ''
    lookup_sources: tuple[str, ...] = field(default_factory=tuple)


VENDOR_PROFILES = {
    'dell': VendorProfile(
        key='dell',
        label='Dell',
        aliases=('dell', 'dell inc', 'dell computers', 'dell technologies'),
        serial_patterns=('^[A-Z0-9]{7}$',),
        notes='Dell service tags are typically 7 characters and often the warranty API is keyed off that value.',
        lookup_sources=('TechDirect warranty API', 'serial/service tag match'),
    ),
    'lenovo': VendorProfile(
        key='lenovo',
        label='Lenovo',
        aliases=('lenovo', 'lenovo inc', 'thinkpad', 'thinkbook', 'ideapad'),
        serial_patterns=('^([A-Z0-9]{7}|[A-Z0-9]{10})$',),
        notes='Lenovo devices often carry serials that are printable as 7- or 10-character strings, but the exact format varies by line.',
        lookup_sources=('vendor warranty portal', 'serial / asset ID match'),
    ),
    'hp': VendorProfile(
        key='hp',
        label='HP',
        aliases=('hp', 'hewlett-packard', 'hewlett packard', 'compaq'),
        serial_patterns=('^[A-Z0-9]{10}$',),
        notes='HP devices commonly identify by 10-character serial strings, though some older assets are more model-specific.',
        lookup_sources=('HP warranty portal', 'system serial match'),
    ),
}


def normalize_vendor_name(value: Optional[str]) -> Optional[str]:
    """Normalize a manufacturer string into the app's canonical vendor key."""
    if value is None:
        return None
    cleaned = value.strip().lower().replace('_', ' ')
    if not cleaned:
        return None
    for key, profile in VENDOR_PROFILES.items():
        if cleaned in profile.aliases or cleaned.startswith(profile.key):
            return key
        if cleaned.startswith('lenovo') or cleaned.startswith('thinkpad'):
            return 'lenovo'
        if 'hewlett' in cleaned or 'compaq' in cleaned:
            return 'hp'
    return None


def vendor_profile_for(vendor: Optional[str]) -> Optional[VendorProfile]:
    key = normalize_vendor_name(vendor)
    return VENDOR_PROFILES.get(key) if key else None


def supported_vendors() -> tuple[str, ...]:
    return SUPPORTED_VENDOR_KEYS


def _serial_matches_pattern(serial: str, pattern: str) -> bool:
    try:
        return bool(re.fullmatch(pattern, serial.strip().upper()))
    except re.error:
        return False


def vendor_from_serial(serial: Optional[str], manufacturer: Optional[str] = None, model: Optional[str] = None,
                      description: Optional[str] = None) -> Optional[str]:
    """Best-effort vendor detection for mixed device fleets.

    Explicit manufacturer data wins. When the record contains no vendor hint,
    serial shape is used as the fallback, and the app prefers conservative
    matches that are consistent with the K-12 field: Dell 7-char service tags,
    HP 10-character serials, and Lenovo when the product text clearly says it.
    """
    if not serial:
        return None

    upper = serial.strip().upper()
    if not upper:
        return None

    explicit = normalize_vendor_name(manufacturer) or normalize_vendor_name(model) or normalize_vendor_name(description)
    if explicit:
        return explicit

    if any(_serial_matches_pattern(upper, pattern) for pattern in VENDOR_PROFILES['dell'].serial_patterns):
        return 'dell'
    if any(_serial_matches_pattern(upper, pattern) for pattern in VENDOR_PROFILES['hp'].serial_patterns):
        return 'hp'

    combined = ' '.join(filter(None, [model, description or '', manufacturer or ''])).lower()
    if 'thinkpad' in combined or 'thinkbook' in combined or 'lenovo' in combined:
        return 'lenovo'
    if re.fullmatch(r'[A-Z0-9]{7}', upper):
        return 'dell'
    return None


def normalize_device_identity(serial: Optional[str], manufacturer: Optional[str] = None,
                             model: Optional[str] = None, description: Optional[str] = None) -> dict:
    """Return the normalized device vendor metadata for a generic asset row."""
    vendor = vendor_from_serial(serial, manufacturer=manufacturer, model=model, description=description)
    return {
        'serial_number': (serial or '').strip(),
        'vendor': vendor,
        'vendor_label': VENDOR_PROFILES[vendor].label if vendor in VENDOR_PROFILES else None,
        'manufacturer_hint': manufacturer.strip() if manufacturer else None,
    }


__all__ = [
    'SUPPORTED_VENDOR_KEYS',
    'VendorProfile',
    'VENDOR_PROFILES',
    'normalize_vendor_name',
    'vendor_profile_for',
    'supported_vendors',
    'vendor_from_serial',
    'normalize_device_identity',
]
