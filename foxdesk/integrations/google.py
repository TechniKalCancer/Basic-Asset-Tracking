"""Google Workspace (Admin SDK): Chromebooks, people, org units, write-back."""
import time
from datetime import datetime, timezone
from foxdesk.core import (
    GOOGLE_ADMIN_IMPERSONATE_EMAIL,
    GOOGLE_LOANER_AUTO_DISABLE_ENABLED,
    GOOGLE_SCOPE_MANAGE,
    GOOGLE_SCOPE_ORGUNIT_READONLY,
    GOOGLE_SCOPE_READONLY,
    GOOGLE_SCOPE_USER_READONLY,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SYNC_ENABLED,
    db,
    logger,
)
from foxdesk.models import Asset, AssetRegistry, GoogleFieldMapping, GoogleOrgUnit, Person
from foxdesk.services.util import _get_nested_value
from foxdesk.services.auth import _log_activity


def _google_directory_service(scopes):
    """
    Builds an authenticated Admin SDK Directory API client using domain-wide
    delegation — the service account impersonates GOOGLE_ADMIN_IMPERSONATE_EMAIL
    (a real Workspace super admin) so its calls act with that admin's authority.
    Raises FileNotFoundError/ValueError from the google-auth library itself if
    GOOGLE_SERVICE_ACCOUNT_FILE doesn't point at a valid key file — callers
    don't need to catch that separately, the routes already flash str(e).
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes,
    ).with_subject(GOOGLE_ADMIN_IMPERSONATE_EMAIL)
    return build('admin', 'directory_v1', credentials=credentials, cache_discovery=False)


def _find_chromeos_device_by_serial(service, serial_number):
    """
    Paginates the domain's Chrome device list looking for a serial number
    match. Filters client-side rather than using the API's `query` parameter —
    serial-number search via `query` isn't reliably documented/stable, while
    paging through `list()` (100 devices/page) is guaranteed-correct at
    typical K-12 fleet sizes. Returns the device dict, or None if not found.
    """
    page_token = None
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=100, pageToken=page_token, projection='FULL',
        ).execute()
        for device in response.get('chromeosdevices', []):
            if device.get('serialNumber') == serial_number:
                return device
        page_token = response.get('nextPageToken')
        if not page_token:
            return None


def sync_chromeos_device_from_google(serial_number):
    """
    Looks up a Chromebook by serial number via the Google Admin SDK Directory API
    and returns its model, org unit, and most recently synced user. Requires a
    Google Cloud service account with domain-wide delegation authorized (in the
    Workspace Admin console) for the
    https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly
    scope, impersonating a super admin (GOOGLE_ADMIN_IMPERSONATE_EMAIL). See
    /admin/google_setup for a step-by-step walkthrough of that setup.

    Args:
        serial_number: The device's manufacturer serial number.

    Returns:
        A dict with keys 'model', 'org_unit', 'recent_user', 'enabled' (True
        when Google's status is 'ACTIVE', False for 'DISABLED'/anything else).

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_READONLY])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    recent_emails = _google_recent_user_emails(device)
    return {
        'model': device.get('model'),
        'org_unit': device.get('orgUnitPath'),
        'recent_user': recent_emails[0] if recent_emails else None,
        'recent_users': recent_emails,
        'last_activity': _parse_google_timestamp(device.get('lastSync')),
        'enabled': device.get('status') == 'ACTIVE',
    }


def _google_recent_user_emails(device):
    """Google's recentUsers for a Chrome device, as lowercased emails, most
    recent sign-in first. Unmanaged (guest/personal) sessions carry no
    email and are skipped — there's no account to match against."""
    emails = []
    for entry in device.get('recentUsers') or []:
        email = (entry.get('email') or '').strip().lower()
        if email and email not in emails:
            emails.append(email)
    return emails[:5]


def _parse_google_timestamp(value):
    """RFC 3339 from the Admin SDK (e.g. '2026-10-01T14:22:10.123Z') to a
    naive UTC datetime, matching every other DateTime column in this app."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def sync_person_from_google(email):
    """
    Looks up a Google Workspace user by email and returns their current org
    unit — same shape/purpose as sync_chromeos_device_from_google() above,
    but for a Person instead of a device. Requires the
    https://www.googleapis.com/auth/admin.directory.user.readonly scope
    (already used for the People sync).

    Args:
        email: The person's Google account email.

    Returns:
        A dict with key 'org_unit'.

    Raises:
        LookupError: No Google account with this email exists in the domain.
    """
    from googleapiclient.errors import HttpError
    service = _google_directory_service([GOOGLE_SCOPE_USER_READONLY])
    try:
        user = service.users().get(userKey=email, projection='basic').execute()
    except HttpError as e:
        if e.resp.status == 404:
            raise LookupError(f'No Google account for "{email}" found in Google Workspace.')
        raise
    return {'org_unit': user.get('orgUnitPath')}


def set_chromeos_device_enabled(serial_number, enabled):
    """
    Enables or disables a Chromebook in Google Workspace by serial number —
    resolves the device ID, then calls the Admin SDK's chromeosdevices().action()
    with action='reenable' (enabled=True) or 'disable' (enabled=False).

    Needs the SAME service account as sync_chromeos_device_from_google() above,
    but with the additional (non-readonly) write scope
    https://www.googleapis.com/auth/admin.directory.device.chromeos authorized
    on its Domain-wide Delegation entry too — not a separate service account,
    just an extra scope on the same Client ID.

    Args:
        serial_number: The device's manufacturer serial number.
        enabled: True to re-enable, False to disable.

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    service.chromeosdevices().action(
        customerId='my_customer', resourceId=device['deviceId'],
        body={'action': 'reenable' if enabled else 'disable'},
    ).execute()


def toggle_chromeos_device_enabled(serial_number):
    """
    Flips a Chromebook's enabled/disabled state to whatever it currently
    isn't — reads Google's live status (not FoxDesk's cached google_enabled,
    which could be stale if the device was changed directly in the Admin
    console) in the same round trip already needed to resolve the device
    ID, then acts on it. Backs the one-click toggle button on the device
    page, so an admin never has to check state before clicking.

    Args:
        serial_number: The device's manufacturer serial number.

    Returns:
        The new enabled state (True/False).

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    currently_enabled = device.get('status') == 'ACTIVE'
    service.chromeosdevices().action(
        customerId='my_customer', resourceId=device['deviceId'],
        body={'action': 'disable' if currently_enabled else 'reenable'},
    ).execute()
    return not currently_enabled


def wipe_chromeos_device_users(serial_number):
    """
    Issues Google's WIPE_USERS remote command to a Chromebook by serial
    number — clears every local user profile/cryptohome on the device
    (the standard remote fix for cryptohome corruption) while leaving it
    enrolled and managed, unlike REMOTE_POWERWASH which fully factory-
    resets and de-enrolls it. There's no way to target just one user's
    profile remotely — this clears all of them on that device.

    Lives under a different Admin SDK resource collection
    (customer().devices().chromeos().issueCommand) than the rest of this
    file's Chrome device calls (the chromeosdevices() resource used by
    list/action/patch), but needs the same write (MANAGE) scope and
    impersonated credentials, so no separate setup is required.

    Fire-and-forget: Google queues the command and executes it
    asynchronously once the device next checks in (it must be online).
    This doesn't poll the returned commandId for completion, matching how
    set_chromeos_device_enabled doesn't confirm completion either.

    Args:
        serial_number: The device's manufacturer serial number.

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    service.customer().devices().chromeos().issueCommand(
        customerId='my_customer', deviceId=device['deviceId'],
        body={'commandType': 'WIPE_USERS'},
    ).execute()


PERSON_SYNC_TARGET_FIELDS = {
    'first_name': 'First Name', 'last_name': 'Last Name',
    'role': 'Role', 'department': 'Department',
}


DEVICE_SYNC_TARGET_FIELDS = {
    'description': 'Description', 'device_type': 'Device Type',
}


ORG_UNIT_SCOPE_STAFF   = '__staff__'


ORG_UNIT_SCOPE_STUDENT = '__student__'


ORG_UNIT_SCOPE_CHOICES = {ORG_UNIT_SCOPE_STAFF: 'Staff (by org unit)', ORG_UNIT_SCOPE_STUDENT: 'Student (by org unit)'}


def _classify_org_unit(org_unit_path):
    """Returns 'staff'/'student' for the given org unit path, based on the
    closest classified ancestor in GoogleOrgUnit (so a sub-OU like
    '/Students/Class of 2030/Section A' inherits its parent's classification
    even if only '/Students' was tagged). Returns None if nothing matches."""
    if not org_unit_path:
        return None
    best_category, best_len = None, -1
    for ou in GoogleOrgUnit.query.filter(GoogleOrgUnit.category.in_(['staff', 'student'])).all():
        path = ou.org_unit_path
        if org_unit_path == path or org_unit_path.startswith(path.rstrip('/') + '/'):
            if len(path) > best_len:
                best_category, best_len = ou.category, len(path)
    return best_category


def _org_unit_site_id(org_unit_path):
    """Returns the Site id tagged on the closest classified ancestor of
    org_unit_path in GoogleOrgUnit (same closest-ancestor logic as
    _classify_org_unit, but for the hand-set site_id instead of category).
    Returns None if no ancestor has a Site tagged."""
    if not org_unit_path:
        return None
    best_site_id, best_len = None, -1
    for ou in GoogleOrgUnit.query.filter(GoogleOrgUnit.site_id.isnot(None)).all():
        path = ou.org_unit_path
        if org_unit_path == path or org_unit_path.startswith(path.rstrip('/') + '/'):
            if len(path) > best_len:
                best_site_id, best_len = ou.site_id, len(path)
    return best_site_id


def _mapping_applies_to_org_unit(mapping, org_unit_path):
    """Whether mapping's org_unit_scope allows it to apply to a record in
    org_unit_path — unscoped (None) mappings always apply; '__staff__'/
    '__student__' apply based on _classify_org_unit(); anything else is
    treated as an exact org unit path (also matching its sub-OUs)."""
    scope = mapping.org_unit_scope
    if not scope:
        return True
    if scope == ORG_UNIT_SCOPE_STAFF:
        return _classify_org_unit(org_unit_path) == 'staff'
    if scope == ORG_UNIT_SCOPE_STUDENT:
        return _classify_org_unit(org_unit_path) == 'student'
    return bool(org_unit_path) and (org_unit_path == scope or org_unit_path.startswith(scope.rstrip('/') + '/'))


def _apply_field_mappings(google_record, obj, mappings):
    """Applies every mapping onto obj (a Person or AssetRegistry instance)
    from the raw Google record. Mappings scoped to a specific org unit or
    org-unit category (see _mapping_applies_to_org_unit) are skipped for
    records outside that scope. Real-column targets are set directly;
    'custom:<key>' targets go into obj.custom_fields. Returns True if
    anything actually changed (so the caller can count real updates, not
    just matches). Does not commit."""
    changed = False
    custom = dict(obj.custom_fields or {})
    org_unit_path = google_record.get('orgUnitPath')
    for m in mappings:
        if not _mapping_applies_to_org_unit(m, org_unit_path):
            continue
        value = _get_nested_value(google_record, m.google_field)
        if value is None:
            continue
        value = str(value)
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


def _fetch_google_org_units():
    """Pulls the full Organizational Unit tree from the Admin SDK (shared by
    both Users and ChromeOS devices) and upserts it into GoogleOrgUnit —
    inserting new paths, refreshing the display name on existing ones, and
    leaving each row's hand-set category untouched. Returns the number of
    org units seen."""
    service = _google_directory_service([GOOGLE_SCOPE_ORGUNIT_READONLY])
    response = service.orgunits().list(customerId='my_customer', type='all').execute()
    org_units = response.get('organizationUnits', [])
    existing = {ou.org_unit_path: ou for ou in GoogleOrgUnit.query.all()}
    for entry in org_units:
        path = entry.get('orgUnitPath')
        if not path:
            continue
        name = entry.get('name')
        if path in existing:
            existing[path].name = name
        else:
            db.session.add(GoogleOrgUnit(org_unit_path=path, name=name))
    db.session.commit()
    return len(org_units)


def _push_loaners_to_ou(site):
    """Moves every loaner-pool device at `site` with a serial number into
    site.loaner_org_unit_path in Google Workspace. Returns (moved, not_found)
    — not_found counts loaner rows whose serial has no matching Chrome
    device in Google (e.g. it's actually a charger, not a Chromebook).
    Raises on auth/API failure so the caller can flash the real error."""
    if not site.loaner_org_unit_path:
        return 0, 0
    rows = AssetRegistry.query.filter_by(is_loaner=True, site_id=site.id) \
        .filter(AssetRegistry.serial_number.isnot(None)).all()
    if not rows:
        return 0, 0
    target_serials = {r.serial_number for r in rows}
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    found = {}
    page_token = None
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=200, pageToken=page_token, projection='BASIC',
        ).execute()
        for d in response.get('chromeosdevices', []):
            sn = d.get('serialNumber')
            if sn in target_serials:
                found[sn] = d['deviceId']
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    not_found = len(target_serials) - len(found)
    device_ids = list(found.values())
    for i in range(0, len(device_ids), 50):  # moveDevicesToOu caps out well under this per call
        service.chromeosdevices().moveDevicesToOu(
            customerId='my_customer', orgUnitPath=site.loaner_org_unit_path,
            body={'deviceIds': device_ids[i:i + 50]},
        ).execute()
    return len(device_ids), not_found


def _auto_create_person_from_google(u, org_unit_path):
    """Creates a new Person from an unmatched Google user record, for
    _run_google_people_sync's auto-create path. Only creates when the
    account is active (not suspended — a suspended account is usually
    departed or was never really enrolled, not a live roster gap) AND its
    org unit classifies cleanly as staff or student via _classify_org_unit
    — role is a required column, and guessing it for an unclassified org
    unit risks misfiling a real person, so those are left unmatched instead
    of guessed. Returns the new (added, uncommitted) Person, or None if the
    account doesn't qualify."""
    if u.get('suspended'):
        return None
    role = _classify_org_unit(org_unit_path)
    if role not in ('staff', 'student'):
        return None
    email = u.get('primaryEmail')
    first_name = (u.get('name') or {}).get('givenName') or ''
    last_name = (u.get('name') or {}).get('familyName') or ''
    if not first_name or not last_name or not email:
        return None
    person = Person(first_name=first_name, last_name=last_name, email=email, role=role,
                     site_id=_org_unit_site_id(org_unit_path))
    db.session.add(person)
    _log_activity('person_add', f'Auto-added {person.full_name} from Google Workspace ({email}).',
                   site_id=person.site_id)
    return person


def _run_google_people_sync():
    """Pulls every Google Workspace user, matches to an existing Person by
    email, and applies each entity_type='person' GoogleFieldMapping onto the
    match — plus, independently of any mapping, corrects site_id from the
    account's org unit if that org unit (or an ancestor) has a Site tagged
    at /admin/google_org_units (see _org_unit_site_id), and caches the org
    unit itself onto Person.google_org_unit so the person edit page can show
    it without a live lookup (see admin_person_google_sync for the
    single-person on-demand version of the same cache). An unmatched
    account is auto-created (see _auto_create_person_from_google) when it
    qualifies; otherwise it's counted as unmatched exactly as before.
    Returns (matched, updated, unmatched_google_accounts, created)."""
    mappings = GoogleFieldMapping.query.filter_by(entity_type='person').all()
    has_site_rules = GoogleOrgUnit.query.filter(GoogleOrgUnit.site_id.isnot(None)).first() is not None
    if not mappings and not has_site_rules:
        return 0, 0, 0, 0
    service = _google_directory_service([GOOGLE_SCOPE_USER_READONLY])
    matched = updated = unmatched = created = 0
    page_token = None
    while True:
        response = service.users().list(
            customer='my_customer', maxResults=200, pageToken=page_token,
        ).execute()
        for u in response.get('users', []):
            email = u.get('primaryEmail')
            org_unit_path = u.get('orgUnitPath')
            person = Person.query.filter_by(email=email).first() if email else None
            if not person:
                person = _auto_create_person_from_google(u, org_unit_path)
                if person:
                    created += 1
                else:
                    unmatched += 1
                    continue
            matched += 1
            row_changed = _apply_field_mappings(u, person, mappings)
            site_id = _org_unit_site_id(org_unit_path)
            if site_id and person.site_id != site_id:
                person.site_id = site_id
                row_changed = True
            if person.google_org_unit != org_unit_path:
                person.google_org_unit = org_unit_path
                row_changed = True
            person.google_last_sync_at = datetime.utcnow()
            if row_changed:
                updated += 1
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    db.session.commit()
    return matched, updated, unmatched, created


def _run_google_device_sync(deadline=None):
    """Pulls every Google Workspace ChromeOS device, matches to an existing
    AssetRegistry row by serial number, and applies each entity_type='device'
    GoogleFieldMapping onto the match — plus, independently of any mapping,
    corrects site_id from the device's org unit the same way
    _run_google_people_sync does for People.

    Also caches model/org unit/recent user/enabled-disabled state onto each
    matched device's Asset row, same fields the per-device 'Sync from
    Google' button fills in — so that info shows up fleet-wide from the
    regular bulk/scheduled sync instead of requiring a click into every
    device one at a time. This runs regardless of whether any field
    mappings or org-unit site rules are configured, since caching this
    snapshot is this sync's job on its own, not just a side effect of
    mapping-driven updates.

    And pushes the other direction too: whenever a matched device's
    annotatedAssetId in Google doesn't match FoxDesk's own asset_tag, it's
    corrected in Google. This is the reconciliation half of the write-back
    that _push_asset_tag_to_google does immediately on Add/Edit Device — it
    catches devices added before that existed, a push that failed at add
    time, or a tag that was later changed directly in Google.

    Uses the write (MANAGE) scope rather than READONLY since it now writes
    annotatedAssetId, and requests projection='FULL' explicitly so
    recentUsers/annotatedAssetId are reliably present regardless of the
    API's undocumented default projection.

    deadline: an optional time.monotonic() cutoff. A first-ever run against
    an existing fleet can need one push per device (nothing has an
    annotatedAssetId yet) — thousands of individual write calls, easily
    minutes of wall-clock time, which blows straight through gunicorn's
    request timeout if triggered from the manual "Sync Devices Now" button.
    When set, the loop stops picking up new devices once past the deadline
    (whatever's already staged is still committed) and reports itself
    truncated rather than getting killed mid-request. The scheduled
    background sync (no HTTP request, no timeout) passes no deadline and
    always finishes the job in one pass, picking up wherever a capped
    manual run left off. Commits every 50 processed devices too, so a
    crash mid-run doesn't lose all progress back to zero.

    Returns (matched, updated, unmatched, pushed, truncated) — updated
    counts AssetRegistry rows actually changed by a mapping or site
    correction; pushed counts devices whose annotatedAssetId was corrected
    in Google. The Asset snapshot cache refreshes on every matched device
    regardless and isn't counted in either, same as the per-device sync
    never counts as an 'update'."""
    mappings = GoogleFieldMapping.query.filter_by(entity_type='device').all()
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    matched = updated = unmatched = pushed = processed = 0
    page_token = None
    now = datetime.utcnow()
    truncated = False
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=200, pageToken=page_token, projection='FULL',
        ).execute()
        for d in response.get('chromeosdevices', []):
            if deadline and time.monotonic() > deadline:
                truncated = True
                break
            serial = d.get('serialNumber')
            row = AssetRegistry.query.filter_by(serial_number=serial).first() if serial else None
            if not row:
                unmatched += 1
                continue
            matched += 1
            row_changed = _apply_field_mappings(d, row, mappings)
            site_id = _org_unit_site_id(d.get('orgUnitPath'))
            if site_id and row.site_id != site_id:
                row.site_id = site_id
                row_changed = True
            if row_changed:
                updated += 1

            asset = Asset.query.filter_by(asset_tag=row.asset_tag).first()
            if not asset:
                asset = Asset(asset_tag=row.asset_tag, is_valid=True)
                db.session.add(asset)
            recent_emails = _google_recent_user_emails(d)
            asset.google_model       = d.get('model')
            asset.google_org_unit    = d.get('orgUnitPath')
            asset.google_recent_user = recent_emails[0] if recent_emails else None
            asset.google_recent_users = recent_emails
            asset.google_last_activity = _parse_google_timestamp(d.get('lastSync'))
            asset.google_enabled     = d.get('status') == 'ACTIVE'
            asset.google_last_sync_at = now

            if d.get('annotatedAssetId') != row.asset_tag:
                try:
                    service.chromeosdevices().patch(
                        customerId='my_customer', deviceId=d['deviceId'],
                        body={'annotatedAssetId': row.asset_tag},
                    ).execute()
                    pushed += 1
                except Exception as e:
                    logger.error('Failed to push asset tag for %s to Google: %s', row.asset_tag, e)

            processed += 1
            if processed % 50 == 0:
                db.session.commit()
        if truncated:
            break
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    db.session.commit()
    return matched, updated, unmatched, pushed, truncated


def _move_device_to_persons_ou(registry_row, person):
    """Looks up person's current Google org unit and moves registry_row's
    Chrome device there, so a device someone is now holding picks up their
    Chrome policies instead of whatever OU it was sitting in before (e.g. a
    loaner pool OU). No-ops if the person has no Google account, or the
    device has no matching Chrome device in Google. Raises on other API
    failures — caller (_sync_device_google_state) catches and logs."""
    service = _google_directory_service([GOOGLE_SCOPE_USER_READONLY, GOOGLE_SCOPE_MANAGE])
    from googleapiclient.errors import HttpError
    try:
        user = service.users().get(userKey=person.email, projection='basic').execute()
    except HttpError as e:
        if e.resp.status == 404:
            return
        raise
    org_unit_path = user.get('orgUnitPath')
    if not org_unit_path:
        return
    device = _find_chromeos_device_by_serial(service, registry_row.serial_number)
    if not device:
        return
    service.chromeosdevices().moveDevicesToOu(
        customerId='my_customer', orgUnitPath=org_unit_path, body={'deviceIds': [device['deviceId']]},
    ).execute()


def move_chromeos_device_to_ou(serial_number, org_unit_path):
    """
    Moves a single Chromebook to a specific Google Workspace org unit path,
    by serial number — the single-device, admin-picked-destination
    counterpart to _move_device_to_persons_ou (destination = a person's own
    OU) and _move_devices_to_ou_for_site (destination = a site's whole
    loaner pool). Backs the 'move_device' automation action.

    Raises LookupError if no matching device exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    service.chromeosdevices().moveDevicesToOu(
        customerId='my_customer', orgUnitPath=org_unit_path, body={'deviceIds': [device['deviceId']]},
    ).execute()


def _sync_device_google_state(registry_row, enabled, person=None):
    """
    Best-effort Google enable/disable for a device on checkout/checkin or
    assignment — and, when enabling to a specific person, also moves the
    device into whichever org unit that person's own Google account
    currently lives in, so a loaner/assigned Chromebook picks up the
    borrower's Chrome policies instead of sitting in a pool/storage OU.

    No-ops (logs and returns) unless GOOGLE_SYNC_ENABLED, the separate
    GOOGLE_LOANER_AUTO_DISABLE_ENABLED env var, AND this device's site's opt-in
    flag are all true, and the device has a serial number on file. Never raises
    and never touches db.session for the enable/disable half — the
    checkout/checkin/assignment has already committed by the time this
    runs, so a Google-side failure shouldn't roll back the local action or
    block the person waiting on it. The OU-move half is separately
    best-effort for the same reason.
    """
    if not (GOOGLE_SYNC_ENABLED and GOOGLE_LOANER_AUTO_DISABLE_ENABLED):
        return
    if not (registry_row.site and registry_row.site.google_loaner_autodisable_enabled):
        return
    if not registry_row.serial_number:
        logger.info('Skipping Google auto-%s for %s: no serial number on file.',
                    'enable' if enabled else 'disable', registry_row.asset_tag)
        return
    try:
        set_chromeos_device_enabled(registry_row.serial_number, enabled)
        asset = Asset.query.filter_by(asset_tag=registry_row.asset_tag).first()
        if asset:
            asset.google_enabled = enabled
            db.session.commit()
    except Exception as e:
        logger.error('Google auto-%s failed for %s: %s',
                     'enable' if enabled else 'disable', registry_row.asset_tag, e)

    if enabled and person and person.email:
        try:
            _move_device_to_persons_ou(registry_row, person)
        except Exception as e:
            logger.error('Failed to move %s into %s\'s org unit: %s', registry_row.asset_tag, person.email, e)


def _push_asset_tag_to_google(registry_row):
    """
    Best-effort write-back of FoxDesk's asset tag into a Chromebook's
    annotatedAssetId field in Google Workspace, by serial number — the push
    half of Google device sync (_run_google_device_sync is the pull half,
    and also reconciles this same field for every device on its own
    schedule, so one added before this existed, or whose push failed here,
    catches up there instead of staying out of sync forever).

    No-ops unless GOOGLE_SYNC_ENABLED and the device has a serial number on
    file — most callers here run right after the registry row's own add/edit
    has already committed, so a Google-side failure shouldn't roll back or
    block that save. Never raises.
    """
    if not (GOOGLE_SYNC_ENABLED and registry_row.serial_number):
        return
    try:
        service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
        device = _find_chromeos_device_by_serial(service, registry_row.serial_number)
        if not device:
            return
        if device.get('annotatedAssetId') == registry_row.asset_tag:
            return
        service.chromeosdevices().patch(
            customerId='my_customer', deviceId=device['deviceId'],
            body={'annotatedAssetId': registry_row.asset_tag},
        ).execute()
    except Exception as e:
        logger.error('Failed to push asset tag for %s to Google: %s', registry_row.asset_tag, e)
