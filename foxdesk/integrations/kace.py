"""Quest KACE SMA inventory sync."""
import re
from foxdesk.core import KACE_ORGANIZATION, KACE_PASSWORD, KACE_URL, KACE_USERNAME, db
from foxdesk.models import AssetRegistry, KaceFieldMapping, Person
from foxdesk.services.util import _generate_asset_tag
from foxdesk.services.auth import _log_activity
from foxdesk.services.assignments import _assign_asset_to_person


def _kace_login_session():
    """
    Logs into the KACE SMA admin console the same way a browser does: GET
    the welcome page for a CSRF token, POST credentials to check_login.php,
    and return the resulting requests.Session (its cookies are what
    authorizes every later request). There's no stable JSON REST API for
    local console accounts on this appliance — /ams/shared/api/security/login
    looks like one but belongs to an unrelated subsystem and rejects every
    real account identically to a fake one, confirmed against this
    installation directly. verify=False matches the appliance's self-signed
    cert, same as every other on-prem admin console in this district.
    Raises RuntimeError with a message safe to flash to the admin UI.
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    try:
        welcome = session.get(f'{KACE_URL}/adminui/welcome.php', verify=False, timeout=15)
        welcome.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f'Could not reach KACE at {KACE_URL}: {e}')
    match = re.search(r'CSRF_TOKEN"\s+value="([^"]*)"', welcome.text)
    if not match:
        raise RuntimeError('Could not find a login form on the KACE welcome page — is KACE_URL correct?')
    try:
        resp = session.post(f'{KACE_URL}/adminui/check_login.php', data={
            'CSRF_TOKEN': match.group(1),
            'LOGIN_NAME': KACE_USERNAME,
            'LOGIN_PASSWORD': KACE_PASSWORD,
            'ORGANIZATION': KACE_ORGANIZATION,
            'save': 'Login',
        }, verify=False, timeout=15, allow_redirects=False)
    except requests.RequestException as e:
        raise RuntimeError(f'KACE login request failed: {e}')
    if 'ERROR_NBR' in resp.headers.get('Location', '') or resp.status_code != 302:
        raise RuntimeError('KACE login failed — check KACE_USERNAME/KACE_PASSWORD/KACE_ORGANIZATION.')
    return session


def _kace_strip_html(value):
    """KACE's inventory grid returns each cell as an HTML fragment like
    '<span title="the real value">the real value, maybe <wbr>-broken</span>'
    rather than a plain value — the title attribute always holds the
    untruncated, unbroken original, so that's what gets extracted. Falls
    back to the raw value for the handful of columns (IDs, booleans) that
    come back as plain strings with no markup at all."""
    if not isinstance(value, str):
        return value
    match = re.search(r'title="([^"]*)"', value)
    return match.group(1) if match else value


def _fetch_kace_devices():
    """
    Pulls every device from the KACE SMA inventory grid
    (/adminui/computer_inventory.php — the same endpoint the admin
    console's own Devices page uses; there's no separate export API),
    paginated via its DataTables-style params. Returns a list of cleaned
    dicts, one per device, each carrying every KACE_DEVICE_FIELDS key plus
    'CSP_ID_NUMBER' (serial number, used for matching — always fetched
    regardless of which mappings are configured, since without it nothing
    can match at all). Raises RuntimeError on any failure.
    """
    import requests
    session = _kace_login_session()
    # CSP_ID_NUMBER (serial), ASSIGNEE_EMAIL, and VIRTUAL are always fetched
    # regardless of which mappings are configured — CSP_ID_NUMBER (with
    # SYSTEM_NAME, already in KACE_DEVICE_FIELDS as the Hostname mapping
    # option) is needed for matching, ASSIGNEE_EMAIL for the independent
    # assignment sync, VIRTUAL to exclude VMs from auto-create — none of the
    # three is itself a mappable target field.
    fields = list(KACE_DEVICE_FIELDS.keys()) + ['CSP_ID_NUMBER', 'ASSIGNEE_EMAIL', 'VIRTUAL']
    devices = []
    start, length = 0, 200
    while True:
        try:
            resp = session.get(f'{KACE_URL}/adminui/computer_inventory.php', params={
                'draw': 1, 'start': start, 'length': length,
            }, headers={'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
               verify=False, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise RuntimeError(f'Failed to read KACE device inventory: {e}')
        rows = payload.get('data', [])
        devices.extend({key: _kace_strip_html(row.get(key)) for key in fields} for row in rows)
        start += length
        if not rows or start >= payload.get('iTotalRecords', 0):
            break
    return devices


def _match_kace_device(d):
    """Matches one cleaned KACE device record to an AssetRegistry row.
    Tries the real hardware serial first (KACE's CSP_ID_NUMBER against
    AssetRegistry.serial_number), then falls back to hostname (KACE's
    SYSTEM_NAME against the same column). The fallback exists because a
    large share of this district's non-Chromebook devices were bulk-
    imported with their hostname in the serial_number column rather than a
    true hardware serial — confirmed directly against production data (0
    matches by real serial, 325 of 347 KACE devices matched by hostname).
    Returns the AssetRegistry row, or None."""
    serial = d.get('CSP_ID_NUMBER')
    if serial:
        row = AssetRegistry.query.filter_by(serial_number=serial).first()
        if row:
            return row
    hostname = d.get('SYSTEM_NAME')
    if hostname:
        return AssetRegistry.query.filter_by(serial_number=hostname).first()
    return None


def _auto_create_kace_registry_row(d, existing_tags):
    """Creates a new AssetRegistry row from an unmatched KACE device, for
    _run_kace_device_sync's auto-create path. Skips virtual machines (KACE's
    VIRTUAL field) — a VM isn't a physical asset this tracker has any use
    for. device_type is inferred from KACE's CHASSIS_TYPE: 'laptop' maps
    directly, everything else (desktop, tablet, blank, etc.) falls back to
    'other' rather than guessing a more specific category that isn't
    actually known. description is set to the device's hostname for
    immediate recognizability — safe to set here (unlike the field-mapping
    path onto an EXISTING row) since this is a brand new row with nothing
    to overwrite. existing_tags is mutated by the caller as rows are
    created, so a single sync run never hands out the same generated tag
    twice. Returns the new (added, uncommitted) AssetRegistry row, or None
    if it doesn't qualify (VM, or no usable serial/hostname at all)."""
    if (d.get('VIRTUAL') or '').strip().lower() == 'yes':
        return None
    serial = d.get('CSP_ID_NUMBER') or d.get('SYSTEM_NAME')
    if not serial:
        return None
    device_type = 'laptop' if d.get('CHASSIS_TYPE') == 'laptop' else 'other'
    tag = _generate_asset_tag(existing_tags)
    hostname = d.get('SYSTEM_NAME')
    row = AssetRegistry(asset_tag=tag, serial_number=serial, device_type=device_type, description=hostname)
    db.session.add(row)
    _log_activity('device_add', f'Auto-added {tag} ({hostname or serial}) from KACE inventory.')
    return row


def _run_kace_device_sync():
    """Pulls every device from KACE SMA and matches each one to an existing
    AssetRegistry row (see _match_kace_device). Applies each
    KaceFieldMapping onto the match — same match-only philosophy as
    _run_google_device_sync for the mapping/enrichment half. Unlike Google
    device sync, an unmatched KACE device IS auto-created as a new registry
    row (see _auto_create_kace_registry_row) rather than left unmatched,
    per an explicit choice made when this was built — Google's Chrome
    device sync stays match-only. Independently of any mapping, also
    assigns a matched (or newly-created) device to whichever Person's email
    matches KACE's ASSIGNEE_EMAIL for it — same "independently of any
    mapping" pattern _run_google_device_sync uses to correct site_id from
    org unit. Silently skips (doesn't count as updated) when there's no
    Person with that email, or the device can't be assigned (e.g. it's in
    the loaner pool) — _assign_asset_to_person already handles both cases
    without raising. Returns (matched, updated, unmatched, created)."""
    mappings = KaceFieldMapping.query.all()
    devices = _fetch_kace_devices()
    existing_tags = {r.asset_tag for r in AssetRegistry.query.with_entities(AssetRegistry.asset_tag).all()}
    matched = updated = unmatched = created = 0
    for d in devices:
        row = _match_kace_device(d)
        if not row:
            row = _auto_create_kace_registry_row(d, existing_tags)
            if not row:
                unmatched += 1
                continue
            existing_tags.add(row.asset_tag)
            created += 1
        matched += 1
        row_changed = _apply_kace_field_mappings(d, row, mappings) if mappings else False
        assignee_email = d.get('ASSIGNEE_EMAIL')
        if assignee_email:
            person = Person.query.filter(db.func.lower(Person.email) == assignee_email.lower()).first()
            if person:
                status, _ = _assign_asset_to_person(row.asset_tag, person)
                if status == 'assigned':
                    row_changed = True
        if row_changed:
            updated += 1
    db.session.commit()
    return matched, updated, unmatched, created


# The KACE inventory grid returns dozens of columns per device — this is
# just the useful subset exposed as mappable sources, picked from a live
# /adminui/computer_inventory.php pull (see _fetch_kace_devices). Unlike
# Google's free-text dotted-path field, KACE fields are offered as a fixed
# dropdown since there's no nesting to traverse and the full column list
# isn't documented anywhere stable enough to expect an admin to type it in.
KACE_DEVICE_FIELDS = {
    'SYSTEM_NAME': 'Hostname', 'OS_NAME': 'Operating System',
    'CS_MANUFACTURER': 'Manufacturer', 'CS_MODEL': 'Model',
    'IP': 'IP Address', 'CHASSIS_TYPE': 'Chassis Type', 'RAM_TOTAL': 'RAM',
    'LAST_INVENTORY': 'Last Inventory (KACE)', 'LAST_SYNC': 'Last Agent Sync (KACE)',
    'ASSET_STATUS': 'Asset Status (KACE)', 'LOCATION': 'Location (KACE)',
}


def _apply_kace_field_mappings(kace_record, obj, mappings):
    """Applies every KaceFieldMapping onto obj (an AssetRegistry instance)
    from a cleaned KACE device record (see _fetch_kace_devices — already a
    flat dict, no dotted-path resolution needed). Same real-column vs
    'custom:<key>' convention as _apply_field_mappings, minus org-unit
    scoping (KACE has no org-unit concept). Returns True if anything
    actually changed. Does not commit."""
    changed = False
    custom = dict(obj.custom_fields or {})
    for m in mappings:
        value = kace_record.get(m.kace_field)
        if not value:
            continue
        if m.target_field.startswith('custom:'):
            key = m.target_field.split(':', 1)[1]
            if custom.get(key) != value:
                custom[key] = value
                changed = True
        elif hasattr(obj, m.target_field) and getattr(obj, m.target_field) != value:
            setattr(obj, m.target_field, value)
            changed = True
    if changed:
        obj.custom_fields = custom
    return changed
