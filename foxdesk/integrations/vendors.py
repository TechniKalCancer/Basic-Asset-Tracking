"""The hardware makers a K-12 district deals with, and telling which one
made a device.

Maker names arrive spelled every which way: "Dell Inc.", "HP Inc.",
"Hewlett-Packard", "LENOVO", "ASUSTeK COMPUTER INC.", or inside a model
string from Google Admin ("Dell Chromebook 3120", "Promethean Chromebox").
normalize_vendor_name() maps all of them to one key per vendor.

Each vendor lists the kinds of equipment it supplies to schools (student
devices, classroom AV, printers, network, servers, power, phones,
security), so the same catalog serves end-user devices today and servers
and infrastructure later.

Telling the maker from the serial alone is a last resort, and only for
shapes that hold up in a real fleet: a 7-character Dell service tag. A
manufacturer that's named but isn't in the catalog stays unrecognized
rather than being guessed from its serial — an Apple serial is often 10
characters, which says nothing about HP.
"""
import re
from dataclasses import dataclass

# What a vendor supplies, for grouping and filtering equipment.
CATEGORIES = {
    'devices': 'Laptops, Chromebooks, desktops and tablets',
    'av': 'Interactive displays, projectors, document cameras and audio',
    'printers': 'Printers and copiers',
    'network': 'Switches, wireless and firewalls',
    'servers': 'Servers and storage',
    'power': 'UPS and power',
    'phones': 'Phones and conferencing',
    'security': 'Cameras and door access',
    'carts': 'Charging carts and lockers',
}


@dataclass(frozen=True)
class VendorProfile:
    key: str
    label: str
    categories: tuple
    aliases: tuple = ()        # whole maker names, as they appear in inventory data
    product_lines: tuple = ()  # model names that only this maker uses ("ThinkPad", "OptiPlex")
    serial_patterns: tuple = ()


def _v(key, label, categories, aliases=(), product_lines=(), serial_patterns=()):
    return VendorProfile(key, label, tuple(categories), tuple(aliases) or (key,), tuple(product_lines),
                         tuple(serial_patterns))


VENDORS = [
    # Student and staff devices
    _v('dell', 'Dell', ['devices', 'servers', 'network'], ['dell', 'dell inc', 'dell technologies', 'dell computer', 'alienware'],
       ['latitude', 'optiplex', 'precision', 'inspiron', 'poweredge', 'wyse', 'powervault'],
       # 7-character service tags: every one in Fox Creek's fleet is a Dell.
       serial_patterns=[r'[0-9A-Z]{7}']),
    _v('hp', 'HP', ['devices', 'printers'], ['hp', 'hp inc', 'hewlett-packard', 'hewlett packard', 'compaq'],
       ['elitebook', 'probook', 'elitedesk', 'prodesk', 'zbook', 'laserjet', 'officejet', 'deskjet']),
    _v('lenovo', 'Lenovo', ['devices', 'servers'], ['lenovo', 'lenovo group'],
       ['thinkpad', 'thinkcentre', 'thinkbook', 'thinkstation', 'ideapad', 'thinksystem']),
    _v('apple', 'Apple', ['devices', 'av'], ['apple', 'apple inc', 'apple computer'],
       ['macbook', 'imac', 'ipad', 'mac mini', 'mac pro', 'apple tv']),
    _v('acer', 'Acer', ['devices', 'av'], ['acer', 'acer inc', 'acer america', 'aopen'],
       ['travelmate', 'aspire', 'veriton']),
    _v('asus', 'ASUS', ['devices', 'network'], ['asus', 'asustek', 'asustek computer'], ['zenbook', 'vivobook', 'expertbook']),
    _v('google', 'Google', ['devices'], ['google', 'google llc'], ['pixelbook']),
    _v('microsoft', 'Microsoft', ['devices'], ['microsoft', 'microsoft corporation'], ['surface']),
    _v('samsung', 'Samsung', ['devices', 'av'], ['samsung', 'samsung electronics'], ['galaxy']),
    _v('ctl', 'CTL', ['devices'], ['ctl', 'ctl corp']),
    _v('dynabook', 'Dynabook (Toshiba)', ['devices'], ['dynabook', 'toshiba']),
    _v('panasonic', 'Panasonic', ['devices', 'av'], ['panasonic'], ['toughbook']),
    # Classroom AV
    _v('promethean', 'Promethean', ['av', 'devices'], ['promethean'], ['activpanel', 'activboard']),
    _v('smart', 'SMART Technologies', ['av'], ['smart technologies'], ['smart board', 'smartboard']),
    _v('newline', 'Newline', ['av'], ['newline', 'newline interactive']),
    _v('boxlight', 'Boxlight (Clevertouch, Mimio)', ['av'], ['boxlight', 'clevertouch', 'mimio']),
    _v('viewsonic', 'ViewSonic', ['av'], ['viewsonic']),
    _v('epson', 'Epson', ['av', 'printers'], ['epson', 'seiko epson'], ['brightlink', 'powerlite']),
    _v('benq', 'BenQ', ['av'], ['benq']),
    _v('optoma', 'Optoma', ['av'], ['optoma']),
    _v('sharp', 'Sharp / NEC', ['av', 'printers'], ['sharp', 'nec', 'sharp nec']),
    _v('lg', 'LG', ['av'], ['lg', 'lg electronics']),
    _v('elmo', 'ELMO', ['av'], ['elmo']),
    _v('ipevo', 'IPEVO', ['av'], ['ipevo']),
    _v('logitech', 'Logitech', ['av', 'phones'], ['logitech']),
    _v('extron', 'Extron', ['av'], ['extron']),
    # Printers and copiers
    _v('kyocera', 'Kyocera', ['printers'], ['kyocera', 'kyocera document solutions', 'kyocera mita'], ['ecosys', 'taskalfa']),
    _v('canon', 'Canon', ['printers', 'av'], ['canon'], ['imagerunner']),
    _v('brother', 'Brother', ['printers'], ['brother', 'brother industries']),
    _v('xerox', 'Xerox', ['printers'], ['xerox'], ['versalink', 'altalink', 'workcentre']),
    _v('ricoh', 'Ricoh', ['printers'], ['ricoh']),
    _v('konica_minolta', 'Konica Minolta', ['printers'], ['konica minolta', 'konica', 'minolta'], ['bizhub']),
    _v('lexmark', 'Lexmark', ['printers'], ['lexmark']),
    _v('dymo', 'DYMO', ['printers'], ['dymo'], ['labelwriter']),
    _v('zebra', 'Zebra', ['printers'], ['zebra', 'zebra technologies']),
    # Network
    _v('cisco', 'Cisco (Meraki)', ['network', 'phones', 'servers'], ['cisco', 'cisco systems', 'meraki', 'cisco meraki'], ['catalyst']),
    _v('hpe', 'HPE (Aruba)', ['network', 'servers'],
       ['hpe', 'hewlett packard enterprise', 'aruba', 'aruba networks', 'aruba a hewlett packard enterprise company'],
       ['proliant']),
    _v('ruckus', 'Ruckus (CommScope)', ['network'], ['ruckus', 'ruckus wireless', 'ruckus networks', 'commscope', 'brocade'],
       ['icx']),
    _v('ubiquiti', 'Ubiquiti', ['network', 'security'], ['ubiquiti', 'ubiquiti networks', 'ubiquiti inc', 'ubnt'], ['unifi']),
    _v('juniper', 'Juniper (Mist)', ['network'], ['juniper', 'juniper networks', 'juniper mist']),
    _v('extreme', 'Extreme Networks', ['network'], ['extreme networks', 'aerohive']),
    _v('fortinet', 'Fortinet', ['network'], ['fortinet'], ['fortigate', 'fortiswitch', 'fortiap']),
    _v('paloalto', 'Palo Alto Networks', ['network'], ['palo alto networks', 'palo alto']),
    _v('sonicwall', 'SonicWall', ['network'], ['sonicwall']),
    _v('netgear', 'NETGEAR', ['network'], ['netgear']),
    _v('tplink', 'TP-Link', ['network'], ['tp-link', 'tplink', 'tp link'], ['omada']),
    # Servers, storage and power
    _v('supermicro', 'Supermicro', ['servers'], ['supermicro', 'super micro computer']),
    _v('synology', 'Synology', ['servers'], ['synology']),
    _v('qnap', 'QNAP', ['servers'], ['qnap']),
    _v('netapp', 'NetApp', ['servers'], ['netapp']),
    _v('apc', 'APC (Schneider Electric)', ['power'], ['apc', 'american power conversion', 'schneider electric'], ['smart-ups']),
    _v('eaton', 'Eaton', ['power'], ['eaton']),
    _v('cyberpower', 'CyberPower', ['power'], ['cyberpower', 'cyber power systems']),
    _v('tripplite', 'Tripp Lite', ['power'], ['tripp lite', 'tripp-lite', 'tripplite']),
    # Phones and conferencing
    _v('yealink', 'Yealink', ['phones'], ['yealink']),
    _v('poly', 'Poly (Polycom)', ['phones', 'av'], ['poly', 'polycom', 'plantronics']),
    _v('grandstream', 'Grandstream', ['phones'], ['grandstream', 'grandstream networks']),
    # Security
    _v('axis', 'Axis Communications', ['security'], ['axis communications', 'axis']),
    _v('verkada', 'Verkada', ['security'], ['verkada']),
    _v('hanwha', 'Hanwha Vision', ['security'], ['hanwha', 'hanwha vision', 'samsung techwin']),
    # Charging carts
    _v('lockncharge', 'LocknCharge', ['carts'], ['lockncharge', 'lock n charge']),
    _v('bretford', 'Bretford', ['carts'], ['bretford']),
]
VENDOR_PROFILES = {v.key: v for v in VENDORS}
SUPPORTED_VENDOR_KEYS = tuple(VENDOR_PROFILES)

# Longest alias first, so "hewlett packard enterprise" wins over "hewlett packard".
_ALIASES = sorted(((alias, v.key) for v in VENDORS for alias in v.aliases), key=lambda a: -len(a[0]))
_PRODUCT_LINES = [(line, v.key) for v in VENDORS for line in v.product_lines]
_CORPORATE = re.compile(r'\b(incorporated|inc|corp|corporation|co|company|ltd|limited|llc|gmbh|plc)\b')


def _clean(value):
    text = re.sub(r'[.,()®™]', ' ', str(value or '').lower()).replace('_', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def normalize_vendor_name(value):
    """The vendor key a maker name or model string names, or None.
    'Dell Inc.' -> 'dell', 'Hewlett Packard Enterprise' -> 'hpe',
    'Dell Chromebook 3120' -> 'dell', 'ThinkPad T14' -> 'lenovo'."""
    text = _clean(value)
    if not text:
        return None
    bare = re.sub(r'\s+', ' ', _CORPORATE.sub(' ', text)).strip()
    for alias, key in _ALIASES:
        for candidate in (text, bare):
            if candidate == alias or candidate.startswith(alias + ' '):
                return key
    for line, key in _PRODUCT_LINES:
        if re.search(rf'(^|\s){re.escape(line)}(\s|$)', text):
            return key
    return None


def vendor_profile_for(vendor):
    key = normalize_vendor_name(vendor) if vendor not in VENDOR_PROFILES else vendor
    return VENDOR_PROFILES.get(key) if key else None


def vendor_label(value):
    """The full display name for a maker ('aruba' -> 'HPE (Aruba)'), or None."""
    profile = vendor_profile_for(value)
    return profile.label if profile else None


def vendor_name(value):
    """The short name to store for a maker ('DELL INC.' -> 'Dell',
    'aruba networks' -> 'HPE'), or None if it isn't in the catalog."""
    label = vendor_label(value)
    return label.split(' (')[0] if label else None


def vendor_names():
    """Short names of every vendor, for a manufacturer field's suggestions."""
    return sorted({v.label.split(' (')[0] for v in VENDORS}, key=str.lower)


def supported_vendors(category=None):
    """Vendor keys, optionally only those that supply `category`."""
    return tuple(v.key for v in VENDORS if category is None or category in v.categories)


def vendor_from_serial(serial, manufacturer=None, model=None, description=None):
    """Best guess at who made a device.

    A named maker wins (the manufacturer field, then the model, then the
    description). If the manufacturer field names someone not in the
    catalog, the answer is None — it isn't any of these. Only when nothing
    names a maker does the serial's shape count, and only for patterns
    that are reliable in practice."""
    for text in (manufacturer, model, description):
        key = normalize_vendor_name(text)
        if key:
            return key
    if _clean(manufacturer):
        return None
    upper = str(serial or '').strip().upper()
    if not upper:
        return None
    for v in VENDORS:
        if any(re.fullmatch(p, upper) for p in v.serial_patterns):
            return v.key
    return None


def normalize_device_identity(serial, manufacturer=None, model=None, description=None):
    """Vendor details for one device record. how is 'named' when the maker
    was named somewhere, 'serial' when it was guessed from the serial, and
    None when it's unknown."""
    named = next((k for k in (normalize_vendor_name(t) for t in (manufacturer, model, description)) if k), None)
    vendor = named or vendor_from_serial(serial, manufacturer=manufacturer, model=model, description=description)
    profile = VENDOR_PROFILES.get(vendor)
    return {
        'serial_number': str(serial or '').strip(),
        'vendor': vendor,
        'vendor_label': profile.label if profile else None,
        'categories': profile.categories if profile else (),
        'how': 'named' if named else ('serial' if vendor else None),
        'manufacturer_hint': manufacturer.strip() if manufacturer else None,
    }


__all__ = [
    'CATEGORIES', 'SUPPORTED_VENDOR_KEYS', 'VendorProfile', 'VENDORS', 'VENDOR_PROFILES',
    'normalize_vendor_name', 'vendor_profile_for', 'vendor_label', 'vendor_name', 'vendor_names', 'supported_vendors',
    'vendor_from_serial', 'normalize_device_identity',
]
