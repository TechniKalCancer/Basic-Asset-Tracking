"""Equipment from LibreNMS.

Everything LibreNMS monitors — switches, access points, firewalls, UPSes,
servers, printers — is read through its API (GET /api/v0/devices with an
X-Auth-Token) and becomes equipment in FoxDesk:

  - each LibreNMS device gets a 'librenms' DeviceRecord, matched to a
    registry device by serial number, then hostname;
  - a device FoxDesk doesn't have yet is added (unless auto_add is off),
    tagged with its system name, typed from what LibreNMS calls it, and
    placed at a school by the site rules (address → school) and in a
    location named after its SNMP location;
  - every sync refreshes what LibreNMS observes (hostname, IP, firmware,
    up or down, last polled). What a person typed — tag, school,
    location, funding, support dates — is never overwritten.

Read-only: nothing is ever written back to LibreNMS.
"""
import ipaddress
import re
from datetime import datetime

import requests

from foxdesk.clock import utcnow
from foxdesk.core import db, logger
from foxdesk.integrations.vendors import normalize_vendor_name, vendor_name
from foxdesk.models import EQUIPMENT_TYPES, Asset, AssetRegistry, DeviceModel, DeviceRecord, LibreNMSSettings, Site
from foxdesk.services.auth import _log_activity
from foxdesk.services.secrets import decrypt_secret, encrypt_secret

TOKEN_PURPOSE = 'librenms-token'

# LibreNMS device type -> FoxDesk equipment type (None: not equipment, skip).
TYPE_MAP = {
    'server': 'server', 'appliance': 'server', 'storage': 'storage', 'network': 'switch', 'wireless': 'access_point',
    'firewall': 'firewall', 'loadbalancer': 'router', 'power': 'ups', 'printer': 'printer', 'camera': 'camera',
    'collaboration': 'phone', 'workstation': None, 'environment': None, 'timing': None,
    'management': None,  # an iDRAC / iLO card: part of a server LibreNMS also lists on its own
}
SKIP_OS = {'ping'}  # monitored by ping alone, so LibreNMS can't say what it is
# Where LibreNMS's type is too broad, its OS says more.
OS_TYPES = {
    'fortigate': 'firewall', 'pfsense': 'firewall', 'opnsense': 'firewall', 'panos': 'firewall', 'sonicwall': 'firewall',
    'asa': 'firewall', 'routeros': 'router', 'vyos': 'router', 'apc': 'ups', 'eatonups': 'ups', 'dsm': 'storage',
    'qnap': 'storage', 'proxmox': 'server', 'idrac': 'server', 'ilo': 'server',
}
OS_VENDORS = {
    'fortigate': 'fortinet', 'fortios': 'fortinet', 'fortiswitch': 'fortinet', 'ironware': 'ruckus',
    'ruckuswireless': 'ruckus', 'ruckuswireless-sz': 'ruckus', 'ruckuswireless-unleashed': 'ruckus', 'ios': 'cisco',
    'iosxe': 'cisco', 'iosxr': 'cisco', 'nxos': 'cisco', 'asa': 'cisco', 'ciscowlc': 'cisco', 'procurve': 'hpe',
    'arubaos': 'hpe', 'arubaos-cx': 'hpe', 'arubainstant': 'hpe', 'ilo': 'hpe', 'unifi': 'ubiquiti', 'edgeos': 'ubiquiti',
    'edgeswitch': 'ubiquiti', 'airos': 'ubiquiti', 'airos-af': 'ubiquiti', 'junos': 'juniper', 'panos': 'paloalto',
    'sonicwall': 'sonicwall', 'apc': 'apc', 'eatonups': 'eaton', 'dsm': 'synology', 'qnap': 'qnap', 'idrac': 'dell',
    'dnos': 'dell', 'dell-os10': 'dell', 'powerconnect': 'dell', 'kyocera': 'kyocera', 'xerox': 'xerox',
    'ricoh': 'ricoh', 'brother': 'brother', 'lexmark': 'lexmark', 'netgear': 'netgear', 'tplink': 'tplink',
    'hiveos-wireless': 'extreme', 'hiveos': 'extreme', 'jetdirect': 'hp', 'drac': 'dell',
}
# What LibreNMS shows when it couldn't name the model: an SNMP object id.
UNNAMED_HARDWARE = re.compile(r'^(registration|enterprises|iso)[.\d]*$|^[\d.]+$', re.IGNORECASE)
# Default SNMP locations that say nothing about where a device is.
PLACEHOLDER_LOCATIONS = {'unknown', 'none', 'n/a', 'not set', 'sitting on the dock of the bay', 'corporate data center'}
CLOSET_TYPES = {'switch', 'firewall', 'router', 'server', 'storage', 'ups'}


class LibreNMSError(Exception):
    """A LibreNMS connection problem, worded for the admin."""


def librenms_settings():
    row = db.session.get(LibreNMSSettings, 1)
    if row is None:
        row = LibreNMSSettings(id=1, verify_tls=True, auto_add=True, import_types=list(EQUIPMENT_TYPES))
        db.session.add(row)
        db.session.flush()
    return row


def librenms_config():
    row = db.session.get(LibreNMSSettings, 1)
    token = decrypt_secret(row.api_token, TOKEN_PURPOSE) if row and row.api_token else None
    url = (row.url or '').strip().rstrip('/') if row else ''
    return dict(url=url, token=token, verify=bool(row.verify_tls) if row else True,
                configured=bool(url and token), token_unreadable=bool(row and row.api_token and not token))


def librenms_configured():
    return librenms_config()['configured']


def save_connection(url, token, verify_tls):
    row = librenms_settings()
    url = (url or '').strip().rstrip('/')
    if url and not re.match(r'^https?://', url):
        raise ValueError('The LibreNMS address starts with http:// or https://.')
    row.url = url or None
    row.verify_tls = bool(verify_tls)
    if token:
        row.api_token = encrypt_secret(token.strip(), TOKEN_PURPOSE)


# ─── site rules ───────────────────────────────────────────────────────────────

def parse_site_rules(text):
    """([(network or prefix, site_id)], [problems]) from lines like
    '10.16.0.0/16 = Fox Creek Middle' or '10.15. = Fox Creek High'."""
    sites = {s.name.strip().lower(): s.id for s in Site.query.all()}
    rules, problems = [], []
    for raw in (text or '').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            problems.append(f'"{raw.strip()}" needs an = between the addresses and the school.')
            continue
        addresses, school = (part.strip() for part in line.split('=', 1))
        site_id = sites.get(school.lower())
        if site_id is None:
            problems.append(f'"{school}" isn\'t one of your schools (Settings → Sites).')
            continue
        try:
            rules.append((ipaddress.ip_network(addresses, strict=False), site_id))
        except ValueError:
            if re.fullmatch(r'[0-9.]+', addresses):
                rules.append((addresses, site_id))
            else:
                problems.append(f'"{addresses}" isn\'t an address range (10.16.0.0/16) or prefix (10.16.).')
    return rules, problems


def site_for_ip(ip, rules):
    try:
        addr = ipaddress.ip_address((ip or '').strip())
    except ValueError:
        return None
    for net, site_id in rules:
        if isinstance(net, str):
            if str(addr).startswith(net):
                return site_id
        elif addr.version == net.version and addr in net:
            return site_id
    return None


# ─── the API ──────────────────────────────────────────────────────────────────

def _get(path, config=None):
    config = config or librenms_config()
    if not config['configured']:
        raise LibreNMSError('LibreNMS isn\'t connected yet.')
    try:
        r = requests.get(f'{config["url"]}/api/v0/{path}', headers={'X-Auth-Token': config['token']},
                         timeout=30, verify=config['verify'])
    except requests.exceptions.SSLError:
        raise LibreNMSError('LibreNMS\'s HTTPS certificate isn\'t trusted. Turn off "Check the certificate" if it\'s '
                            'self-signed, or use its http:// address.')
    except requests.RequestException as e:
        raise LibreNMSError(f'Couldn\'t reach LibreNMS at {config["url"]}: {e.__class__.__name__}.')
    if r.status_code in (401, 403):
        raise LibreNMSError('LibreNMS rejected the API token. Make a new one in LibreNMS (your user menu → API).')
    if r.status_code >= 400:
        raise LibreNMSError(f'LibreNMS answered {r.status_code} for {path}.')
    try:
        data = r.json()
    except ValueError:
        raise LibreNMSError('That address answered, but not like LibreNMS. Check the URL (no /api at the end).')
    if data.get('status') not in (None, 'ok'):
        raise LibreNMSError(f'LibreNMS said: {data.get("message") or data.get("status")}')
    return data


def check_connection():
    """(device count, LibreNMS version) or raises LibreNMSError."""
    devices = _get('devices').get('devices') or []
    try:
        version = (_get('system').get('system') or [{}])[0].get('local_ver')
    except LibreNMSError:
        version = None
    return len(devices), version


def foxdesk_type(device):
    os_name = (device.get('os') or '').lower()
    if os_name in SKIP_OS:
        return None
    return OS_TYPES.get(os_name) or TYPE_MAP.get((device.get('type') or '').lower(), None if device.get('type') else 'switch')


def hardware(device):
    """The model name LibreNMS reports, or None when it's only an object id."""
    text = (device.get('hardware') or '').strip()
    return None if not text or UNNAMED_HARDWARE.match(text) else text


def _site_names(sites):
    """[(site_id, the lowercase names and initials it goes by)]: 'fox creek
    middle school', 'fox creek middle', 'fcms'."""
    out = []
    for s in sites:
        words = s.name.strip().lower().split()
        names = {' '.join(words), ' '.join(w for w in words if w != 'school'), ''.join(w[0] for w in words)}
        out.append((s.id, {n for n in names if n}))
    return out


def site_from_text(text, site_names):
    """The school a location string names ('Fox Creek High School',
    'FCMS MDF'), or None."""
    for part in re.split(r'[|/,]', (text or '').lower()):
        part = part.strip()
        for site_id, names in site_names:
            if any(part == n or part.startswith(n + ' ') for n in names):
                return site_id
    return None


def location_parts(text, site_names):
    """The places in a location string, outermost first, leaving out what
    only names the school: 'Fox Creek Middle School|Fox Creek Middle|FCMS'
    -> [], 'FCMS MDF' -> ['MDF'], 'Room 204' -> ['Room 204']."""
    names = {n for _, ns in site_names for n in ns}
    parts = []
    for part in (text or '').split('|'):
        part = part.strip()
        low = part.lower()
        if not part or low in PLACEHOLDER_LOCATIONS or low in names:
            continue
        for n in sorted(names, key=len, reverse=True):
            if low.startswith(n + ' '):
                part = part[len(n):].strip(' -:')
                break
        if part:
            parts.append(part[:120])
    return parts


def device_vendor(device):
    for text in (hardware(device), device.get('sysDescr')):
        key = normalize_vendor_name(text)
        if key:
            return key
    return OS_VENDORS.get((device.get('os') or '').lower())


def _when(value):
    try:
        return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S') if value else None
    except ValueError:
        return None


def _clean_serial(value):
    serial = re.sub(r'\s+', '', str(value or ''))
    return serial if len(serial) >= 4 and serial.lower() not in ('unknown', 'none', 'null', 'n/a', '0', 'default') else None


def _name(device):
    """The short system name ('fcms-core-sw1', not 'fcms-core-sw1.district.org')."""
    name = (device.get('sysName') or device.get('hostname') or '').strip()
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        return name.split('.')[0] if name else ''


def _unique_tag(base, device_id):
    tag = re.sub(r'[^A-Za-z0-9._-]+', '-', base or '').strip('-')[:100] or f'LIBRENMS-{device_id}'
    if AssetRegistry.query.filter(db.func.lower(AssetRegistry.asset_tag) == tag.lower()).first():
        tag = f'{tag}-{device_id}'
    return tag


def _device_model(vendor_key, hardware, device_type):
    """The DeviceModel for this maker + model, made if needed."""
    maker, model = vendor_name(vendor_key), (hardware or '').strip()[:120]
    if not maker or not model:
        return None
    if model.lower().startswith(maker.lower() + ' '):
        model = model[len(maker) + 1:]
    found = DeviceModel.query.filter(db.func.lower(DeviceModel.manufacturer) == maker.lower(),
                                     db.func.lower(DeviceModel.model_name) == model.lower()).first()
    if found is None:
        found = DeviceModel(manufacturer=maker, model_name=model, device_type=device_type)
        db.session.add(found)
        db.session.flush()
    return found


def apply_device(device, settings, rules, config, site_names=()):
    """Bring one LibreNMS device into FoxDesk. Returns 'added', 'linked',
    'updated', 'unmatched' or 'skipped'. Does not commit."""
    from foxdesk.services.equipment import find_or_create_location
    from foxdesk.services.identities import place_device_record
    if str(device.get('disabled') or '0') in ('1', 'True', 'true'):
        return 'skipped'
    ftype = foxdesk_type(device)
    if ftype is None or ftype not in (settings.import_types or EQUIPMENT_TYPES):
        return 'skipped'
    device_id = device.get('device_id')
    name, serial = _name(device), _clean_serial(device.get('serial'))
    ip = (device.get('ip') or '').strip() or None
    if not ip:
        try:
            ip = str(ipaddress.ip_address((device.get('hostname') or '').strip()))
        except ValueError:
            ip = None
    up = str(device.get('status')) in ('1', 'True', 'true')
    known = DeviceRecord.query.filter_by(source='librenms', external_key=str(device_id)).first()
    was_linked = bool(known and known.registry_id)
    rec, row = place_device_record(
        'librenms', device_id, serial_number=serial, hostname=name or None,
        os_name=device.get('os'), os_version=device.get('version'), enabled=True, last_seen_at=_when(device.get('last_polled')),
        raw={'up': up, 'status_reason': device.get('status_reason') or None, 'type': device.get('type'),
             'hardware': device.get('hardware'), 'location': device.get('location'), 'ip': ip,
             'uptime': device.get('uptime'), 'url': f'{config["url"]}/device/device={device_id}/'})
    outcome = ('updated' if was_linked else 'linked') if row else None
    if row is None:
        if rec.review_status == 'ignored':
            return 'skipped'
        if not settings.auto_add:
            return 'unmatched'
        taken = serial and AssetRegistry.query.filter(db.func.upper(AssetRegistry.serial_number) == serial.upper()).first()
        row = AssetRegistry(asset_tag=_unique_tag(name or ip, device_id), serial_number=None if taken else serial,
                            device_type=ftype, description=(hardware(device) or '')[:255] or None)
        db.session.add(row)
        db.session.flush()
        if not Asset.query.filter_by(asset_tag=row.asset_tag).first():
            db.session.add(Asset(asset_tag=row.asset_tag, is_valid=True, status='available'))
        rec.registry_id = row.id
        _log_activity('device_add', f'Added {row.asset_tag} from LibreNMS ({ftype.replace("_", " ")}).')
        outcome = 'added'

    # What LibreNMS observes is always current; what people typed stays.
    row.hostname = name or row.hostname
    row.ip_address = ip or row.ip_address
    row.firmware = (device.get('version') or '')[:120] or row.firmware
    if not row.serial_number and serial and not AssetRegistry.query.filter(
            db.func.upper(AssetRegistry.serial_number) == serial.upper(), AssetRegistry.id != row.id).first():
        row.serial_number = serial
    if row.device_model_id is None:
        model = _device_model(device_vendor(device), hardware(device), row.device_type)
        row.device_model_id = model.id if model else None
    if row.site_id is None:
        row.site_id = site_for_ip(ip, rules) or site_from_text(device.get('location'), site_names)
    if row.location_id is None and row.site_id:
        loc = None
        for i, name in enumerate(location_parts(device.get('location'), site_names)):
            kind = 'rack' if 'rack' in name.lower() else ('closet' if row.device_type in CLOSET_TYPES and i == 0 else 'room')
            loc = find_or_create_location(row.site_id, name, kind=kind, parent=loc)
        row.location_id = loc.id if loc else None
    return outcome


def run_librenms_sync():
    """Sync every LibreNMS device. Returns a summary dict; records the run
    on the settings row. Commits."""
    settings = librenms_settings()
    config = librenms_config()
    rules, _ = parse_site_rules(settings.site_rules)
    site_names = _site_names(Site.query.all())
    summary = {'seen': 0, 'added': 0, 'linked': 0, 'updated': 0, 'unmatched': 0, 'skipped': 0, 'failed': 0}
    try:
        devices = _get('devices', config).get('devices') or []
    except LibreNMSError as e:
        settings.last_run_at, settings.last_error = utcnow(), str(e)
        db.session.commit()
        raise
    for device in devices:
        summary['seen'] += 1
        try:
            with db.session.begin_nested():
                summary[apply_device(device, settings, rules, config, site_names)] += 1
        except Exception as e:  # one odd device mustn't stop the rest
            logger.warning('LibreNMS device %s failed: %s', device.get('device_id'), e)
            summary['failed'] += 1
    settings = librenms_settings()
    settings.last_run_at, settings.last_summary, settings.last_error = utcnow(), summary, None
    _log_activity('scheduled_sync', f'LibreNMS sync: {describe_summary(summary)}')
    db.session.commit()
    return summary


def describe_summary(s):
    if not s:
        return 'Not run yet.'
    parts = [f'{s.get("seen", 0)} devices in LibreNMS', f'{s.get("added", 0)} added', f'{s.get("linked", 0)} matched to '
             f'devices you had', f'{s.get("updated", 0)} updated']
    if s.get('unmatched'):
        parts.append(f'{s["unmatched"]} not matched (adding is off)')
    if s.get('skipped'):
        parts.append(f'{s["skipped"]} skipped')
    if s.get('failed'):
        parts.append(f'{s["failed"]} failed')
    return ', '.join(parts) + '.'
