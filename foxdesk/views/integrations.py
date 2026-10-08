"""Pages: integrations."""
import time
from datetime import datetime
from flask import flash, redirect, render_template, request, url_for
from foxdesk.core import (
    AD_SYNC_ENABLED,
    GOOGLE_ADMIN_IMPERSONATE_EMAIL,
    GOOGLE_LOANER_AUTO_DISABLE_ENABLED,
    GOOGLE_SCOPE_READONLY,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SYNC_ENABLED,
    KACE_ORGANIZATION,
    KACE_SYNC_ENABLED,
    KACE_URL,
    KACE_USERNAME,
    app,
    db,
)
from foxdesk.models import (
    Asset,
    AssetRegistry,
    CustomField,
    GoogleFieldMapping,
    GoogleOrgUnit,
    KaceFieldMapping,
    Site,
)
from foxdesk.services.auth import _current_site_ids, _log_activity, require_permission, require_super_admin
from foxdesk.services.scoping import _scope_registry
from foxdesk.integrations.google import (
    DEVICE_SYNC_TARGET_FIELDS,
    ORG_UNIT_SCOPE_CHOICES,
    PERSON_SYNC_TARGET_FIELDS,
    _fetch_google_org_units,
    _google_directory_service,
    _push_loaners_to_ou,
    _run_google_device_sync,
    _run_google_people_sync,
    sync_chromeos_device_from_google,
    toggle_chromeos_device_enabled,
)
from foxdesk.integrations.kace import KACE_DEVICE_FIELDS, _fetch_kace_devices, _run_kace_device_sync
from foxdesk.services.scheduler import SYNC_SCHEDULE_INTERVALS, _get_or_create_sync_schedule


@app.route('/admin/assets/<string:asset_tag>/google_sync', methods=['POST'])
@require_permission('devices')
def admin_asset_google_sync(asset_tag):
    """Pulls model/org-unit/recent-user from Google Workspace for one asset, by serial number."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()

    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet. Set GOOGLE_SERVICE_ACCOUNT_FILE '
              'and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env to enable it.', 'info')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    if not registry_row.serial_number:
        flash(f'{asset_tag} has no serial number on file to look up.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    try:
        info = sync_chromeos_device_from_google(registry_row.serial_number)
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.google_model        = info.get('model')
        asset.google_org_unit     = info.get('org_unit')
        asset.google_recent_user  = info.get('recent_user')
        asset.google_recent_users = info.get('recent_users')
        asset.google_last_activity = info.get('last_activity')
        asset.google_enabled      = info.get('enabled')
        asset.google_last_sync_at = datetime.utcnow()
        db.session.commit()
        flash(f'Synced {asset_tag} from Google.', 'success')
    except LookupError as e:
        flash(str(e), 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Google sync failed: {e}', 'error')

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/assets/<string:asset_tag>/google_toggle', methods=['POST'])
@require_permission('devices')
def admin_asset_google_toggle(asset_tag):
    """One-click flip of a device's Google Workspace enabled/disabled state —
    always the opposite of whatever it currently is, so the button on the
    device page never requires checking state first."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()

    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet. Set GOOGLE_SERVICE_ACCOUNT_FILE '
              'and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env to enable it.', 'info')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    if not registry_row.serial_number:
        flash(f'{asset_tag} has no serial number on file to look up.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    try:
        new_enabled = toggle_chromeos_device_enabled(registry_row.serial_number)
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.google_enabled = new_enabled
        asset.google_last_sync_at = datetime.utcnow()
        _log_activity('device_google_toggle',
                       f'{"Enabled" if new_enabled else "Disabled"} {asset_tag} in Google Workspace.',
                       site_id=registry_row.site_id)
        db.session.commit()
        flash(f'{asset_tag} is now {"enabled" if new_enabled else "disabled"} in Google Workspace.', 'success')
    except LookupError as e:
        flash(str(e), 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update Google Workspace status: {e}', 'error')

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/google_setup')
@require_super_admin
def admin_google_setup():
    """
    Guided setup checklist for Google Workspace sync. The Google Cloud
    Console / Workspace Admin steps genuinely can't be automated from here —
    Google requires a human super admin to authorize Domain-wide Delegation
    in the Admin console, and no API exists for that step (GAM can't
    automate it either) — so this is a checklist with direct links plus a
    live connectivity test at the end, not a wizard that does the work for you.
    """
    return render_template('admin_google_setup.html',
                           google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           google_loaner_autodisable_enabled=GOOGLE_LOANER_AUTO_DISABLE_ENABLED,
                           service_account_file=GOOGLE_SERVICE_ACCOUNT_FILE,
                           impersonate_email=GOOGLE_ADMIN_IMPERSONATE_EMAIL)


@app.route('/admin/google_setup/test', methods=['POST'])
@require_super_admin
def admin_google_setup_test():
    """
    A live round-trip against the Admin SDK Directory API — the only way to
    actually confirm the Google Cloud Console + Workspace Admin steps worked.
    GOOGLE_SYNC_ENABLED (used everywhere else) is just an env-var-presence
    check, not proof the credentials/delegation are actually valid.
    """
    if not GOOGLE_SYNC_ENABLED:
        flash('Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env and restart before testing.', 'error')
        return redirect(url_for('admin_google_setup'))
    try:
        service = _google_directory_service([GOOGLE_SCOPE_READONLY])
        response = service.chromeosdevices().list(customerId='my_customer', maxResults=1).execute()
        if response.get('chromeosdevices'):
            flash('Connected to Google Workspace successfully — found at least one Chrome device on file.', 'success')
        else:
            flash('Connected to Google Workspace successfully, but no Chrome devices were found — '
                  'double-check GOOGLE_ADMIN_IMPERSONATE_EMAIL is a real super admin on this domain.', 'info')
    except Exception as e:
        flash(f'Connection failed: {e}', 'error')
    return redirect(url_for('admin_google_setup'))


@app.route('/admin/google_org_units')
@require_super_admin
def admin_google_org_units():
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    sites = Site.query.order_by(Site.name).all()
    return render_template('admin_google_org_units.html', org_units=org_units, sites=sites,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_org_units/refresh', methods=['POST'])
@require_super_admin
def admin_google_org_units_refresh():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_org_units'))
    try:
        count = _fetch_google_org_units()
        _log_activity('org_unit_refresh', f'Refreshed org unit list from Google: {count} found.')
        db.session.commit()  # _fetch_google_org_units() already committed its own changes; this just persists the log entry above, added after that commit
        flash(f'Pulled {count} org unit(s) from Google Workspace.', 'success')
    except Exception as e:
        flash(f'Refresh failed: {e}', 'error')
    return redirect(url_for('admin_google_org_units'))


@app.route('/admin/google_org_units/save', methods=['POST'])
@require_super_admin
def admin_google_org_units_save():
    valid_site_ids = {s.id for s in Site.query.all()}
    changed = 0
    for ou in GoogleOrgUnit.query.all():
        category = request.form.get(f'category_{ou.id}', 'unclassified')
        if category not in ('unclassified', 'staff', 'student'):
            category = 'unclassified'
        if category != ou.category:
            ou.category = category
            changed += 1

        site_raw = request.form.get(f'site_{ou.id}', '').strip()
        site_id = int(site_raw) if site_raw.isdigit() and int(site_raw) in valid_site_ids else None
        if site_id != ou.site_id:
            ou.site_id = site_id
            changed += 1
    if changed:
        _log_activity('org_unit_classify', f'Reclassified/re-sited {changed} org unit field(s).')
        db.session.commit()
        flash(f'Saved — {changed} change(s).', 'success')
    else:
        flash('No changes to save.', 'info')
    return redirect(url_for('admin_google_org_units'))


@app.route('/admin/google_ou_push')
@require_super_admin
def admin_google_ou_push():
    """The reverse of Org Units' site-tagging: here FoxDesk's own Site
    decides where a site's loaner Chromebooks belong in Google, and this
    page pushes them there — see _push_loaners_to_ou(). Deliberately
    devices-only: FoxDesk doesn't push people's Google accounts between org
    units, only reads them (see _run_google_people_sync/_org_unit_site_id)."""
    sites = Site.query.order_by(Site.name).all()
    loaner_counts = {
        row[0]: row[1] for row in db.session.query(AssetRegistry.site_id, db.func.count(AssetRegistry.id))
        .filter(AssetRegistry.is_loaner.is_(True), AssetRegistry.serial_number.isnot(None))
        .group_by(AssetRegistry.site_id)
    }
    return render_template('admin_google_ou_push.html', sites=sites, loaner_counts=loaner_counts,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_ou_push/loaners/<int:site_id>', methods=['POST'])
@require_super_admin
def admin_google_ou_push_loaners(site_id):
    site = Site.query.get_or_404(site_id)
    if not site.loaner_org_unit_path:
        flash(f'Set a Loaner Org Unit on {site.name} first (under Sites) before pushing.', 'error')
        return redirect(url_for('admin_google_ou_push'))
    try:
        moved, not_found = _push_loaners_to_ou(site)
        _log_activity('loaner_ou_push', f'Pushed {moved} loaner(s) from {site.name} to {site.loaner_org_unit_path}.',
                       site_id=site.id)
        db.session.commit()  # _push_loaners_to_ou() makes no local DB changes (it only calls the Google API); this persists the log entry above
        msg = f'Moved {moved} device(s) to {site.loaner_org_unit_path}.'
        if not_found:
            msg += f' {not_found} loaner(s) had no matching Chrome device in Google (e.g. a charger, not a Chromebook).'
        flash(msg, 'success' if moved else 'info')
    except Exception as e:
        flash(f'Push failed: {e}', 'error')
    return redirect(url_for('admin_google_ou_push'))


@app.route('/admin/google_field_mapping')
@require_super_admin
def admin_google_field_mapping():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    mappings = GoogleFieldMapping.query.filter_by(entity_type=entity_type).order_by(GoogleFieldMapping.google_field).all()
    custom_fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    real_fields = PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    return render_template('admin_google_field_mapping.html', mappings=mappings, entity_type=entity_type,
                           custom_fields=custom_fields, real_fields=real_fields,
                           org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_field_mapping/new', methods=['GET', 'POST'])
@require_super_admin
def admin_google_field_mapping_new():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    custom_fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    real_fields = PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()

    if request.method == 'POST':
        entity_type = request.form.get('entity_type', entity_type)
        google_field = request.form.get('google_field', '').strip()
        target_field = request.form.get('target_field', '').strip()
        org_unit_scope = request.form.get('org_unit_scope', '').strip() or None
        valid_targets = set((PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS).keys())
        valid_targets |= {f'custom:{c.field_key}' for c in CustomField.query.filter_by(entity_type=entity_type).all()}
        valid_scopes = set(ORG_UNIT_SCOPE_CHOICES.keys()) | {ou.org_unit_path for ou in org_units}

        if not google_field:
            flash('Enter the Google field to pull from (e.g. orgUnitPath).', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)
        if target_field not in valid_targets:
            flash('Choose a valid target field.', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)
        if org_unit_scope is not None and org_unit_scope not in valid_scopes:
            flash('Choose a valid org unit scope.', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)

        db.session.add(GoogleFieldMapping(entity_type=entity_type, google_field=google_field,
                                           target_field=target_field, org_unit_scope=org_unit_scope))
        scope_note = f' (scoped to {org_unit_scope})' if org_unit_scope else ''
        _log_activity('google_field_mapping_add', f'Mapped Google "{google_field}" -> "{target_field}" ({entity_type}){scope_note}.')
        db.session.commit()
        flash('Mapping added.', 'success')
        return redirect(url_for('admin_google_field_mapping', entity=entity_type))

    return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                           custom_fields=custom_fields, real_fields=real_fields,
                           org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=None)


@app.route('/admin/google_field_mapping/<int:mapping_id>/delete', methods=['POST'])
@require_super_admin
def admin_google_field_mapping_delete(mapping_id):
    mapping = GoogleFieldMapping.query.get_or_404(mapping_id)
    entity_type = mapping.entity_type
    _log_activity('google_field_mapping_delete', f'Removed mapping "{mapping.google_field}" -> "{mapping.target_field}" ({entity_type}).')
    db.session.delete(mapping)
    db.session.commit()
    flash('Mapping removed.', 'success')
    return redirect(url_for('admin_google_field_mapping', entity=entity_type))


@app.route('/admin/google_field_mapping/sync/people', methods=['POST'])
@require_super_admin
def admin_google_sync_people():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_field_mapping', entity='person'))
    try:
        matched, updated, unmatched, created, held = _run_google_people_sync()
        _log_activity('google_field_sync',
                       f'Synced People from Google: {matched} matched, {updated} updated, {created} auto-created, '
                       f'{held} held for review.')
        db.session.commit()  # _run_google_people_sync() already committed its own changes; this just persists the log entry above, added after that commit
        flash(f'{matched} matched, {updated} updated, {created} auto-created. '
              f'{held} account(s) only matched someone\'s name and are waiting on Accounts to Review. '
              f'{unmatched} Google account(s) had no matching Person and didn\'t qualify to auto-create '
              f'(suspended, or an unclassified org unit).',
              'success' if matched else 'info')
    except Exception as e:
        flash(f'Sync failed: {e}', 'error')
    return redirect(url_for('admin_google_field_mapping', entity='person'))


@app.route('/admin/google_field_mapping/sync/devices', methods=['POST'])
@require_super_admin
def admin_google_sync_devices():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_field_mapping', entity='device'))
    try:
        # Capped well under gunicorn's request timeout — a first-ever run
        # against an existing fleet can need one push per device, which
        # can't finish inside a single request. The scheduled background
        # sync (see SyncSchedule) has no such cap and will finish the job.
        deadline = time.monotonic() + 45
        matched, updated, unmatched, pushed, truncated = _run_google_device_sync(deadline=deadline)
        _log_activity('google_field_sync', f'Synced Devices from Google: {matched} matched, {updated} updated, {pushed} asset tag(s) pushed to Google.')
        db.session.commit()  # _run_google_device_sync() already committed its own changes; this just persists the log entry above, added after that commit
        message = f'{matched} matched, {updated} updated, {pushed} asset tag(s) pushed to Google. {unmatched} Google device(s) had no matching registry serial number.'
        if truncated:
            message += ' Stopped early to stay within the request time limit — click "Run Sync Now" again to continue, or let the scheduled sync finish it overnight.'
        flash(message, 'success' if matched else 'info')
    except Exception as e:
        flash(f'Sync failed: {e}', 'error')
    return redirect(url_for('admin_google_field_mapping', entity='device'))


@app.route('/admin/kace_setup')
@require_super_admin
def admin_kace_setup():
    """Status page for the KACE SMA integration — env-var-driven, no OAuth/
    service-account dance like Google, so this is simpler than
    /admin/google_setup: just confirms KACE_URL/KACE_USERNAME/KACE_PASSWORD
    are set and offers a live connectivity test."""
    return render_template('admin_kace_setup.html', kace_sync_enabled=KACE_SYNC_ENABLED,
                           kace_url=KACE_URL, kace_username=KACE_USERNAME,
                           kace_organization=KACE_ORGANIZATION)


@app.route('/admin/kace_setup/test', methods=['POST'])
@require_super_admin
def admin_kace_setup_test():
    """A live round-trip against KACE — the only way to actually confirm
    the credentials/URL/org are correct. KACE_SYNC_ENABLED (used everywhere
    else) is just an env-var-presence check, not proof they're valid."""
    if not KACE_SYNC_ENABLED:
        flash('Set KACE_URL, KACE_USERNAME, and KACE_PASSWORD in .env and restart before testing.', 'error')
        return redirect(url_for('admin_kace_setup'))
    try:
        devices = _fetch_kace_devices()
        with_serial = sum(1 for d in devices if d.get('CSP_ID_NUMBER'))
        flash(f'Connected to KACE successfully — found {len(devices)} device(s) in inventory '
              f'({with_serial} with a serial number on file).', 'success')
    except Exception as e:
        flash(f'Connection failed: {e}', 'error')
    return redirect(url_for('admin_kace_setup'))


@app.route('/admin/kace_field_mapping')
@require_super_admin
def admin_kace_field_mapping():
    mappings = KaceFieldMapping.query.order_by(KaceFieldMapping.kace_field).all()
    custom_fields = CustomField.query.filter_by(entity_type='device').order_by(CustomField.label).all()
    return render_template('admin_kace_field_mapping.html', mappings=mappings,
                           custom_fields=custom_fields, real_fields=DEVICE_SYNC_TARGET_FIELDS,
                           kace_fields=KACE_DEVICE_FIELDS, kace_sync_enabled=KACE_SYNC_ENABLED)


@app.route('/admin/kace_field_mapping/new', methods=['POST'])
@require_super_admin
def admin_kace_field_mapping_new():
    kace_field = request.form.get('kace_field', '').strip()
    target_field = request.form.get('target_field', '').strip()
    valid_targets = set(DEVICE_SYNC_TARGET_FIELDS.keys())
    valid_targets |= {f'custom:{c.field_key}' for c in CustomField.query.filter_by(entity_type='device').all()}

    if kace_field not in KACE_DEVICE_FIELDS:
        flash('Choose a valid KACE field.', 'error')
    elif target_field not in valid_targets:
        flash('Choose a valid target field.', 'error')
    else:
        db.session.add(KaceFieldMapping(kace_field=kace_field, target_field=target_field))
        _log_activity('kace_field_mapping_add', f'Mapped KACE "{kace_field}" -> "{target_field}".')
        db.session.commit()
        flash('Mapping added.', 'success')
    return redirect(url_for('admin_kace_field_mapping'))


@app.route('/admin/kace_field_mapping/<int:mapping_id>/delete', methods=['POST'])
@require_super_admin
def admin_kace_field_mapping_delete(mapping_id):
    mapping = KaceFieldMapping.query.get_or_404(mapping_id)
    _log_activity('kace_field_mapping_delete', f'Removed mapping "{mapping.kace_field}" -> "{mapping.target_field}".')
    db.session.delete(mapping)
    db.session.commit()
    flash('Mapping removed.', 'success')
    return redirect(url_for('admin_kace_field_mapping'))


@app.route('/admin/kace_field_mapping/sync', methods=['POST'])
@require_super_admin
def admin_kace_sync_devices():
    if not KACE_SYNC_ENABLED:
        flash('KACE sync isn\'t configured yet — see /admin/kace_setup.', 'info')
        return redirect(url_for('admin_kace_field_mapping'))
    try:
        matched, updated, unmatched, created = _run_kace_device_sync()
        _log_activity('kace_field_sync',
                       f'Synced Devices from KACE: {matched} matched, {updated} updated, {created} auto-created.')
        db.session.commit()
        flash(f'{matched} matched, {updated} updated, {created} auto-created. '
              f'{unmatched} KACE device(s) skipped (virtual machines, or no usable serial/hostname).',
              'success' if matched else 'info')
    except Exception as e:
        flash(f'Sync failed: {e}', 'error')
    return redirect(url_for('admin_kace_field_mapping'))


@app.route('/admin/sync_schedule')
@require_super_admin
def admin_sync_schedule():
    person_schedule = _get_or_create_sync_schedule('person')
    device_schedule = _get_or_create_sync_schedule('device')
    kace_schedule = _get_or_create_sync_schedule('kace')
    ad_schedule = _get_or_create_sync_schedule('ad')
    return render_template('admin_sync_schedule.html', person_schedule=person_schedule, device_schedule=device_schedule,
                           kace_schedule=kace_schedule, ad_schedule=ad_schedule, intervals=SYNC_SCHEDULE_INTERVALS,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED, kace_sync_enabled=KACE_SYNC_ENABLED,
                           ad_sync_enabled=AD_SYNC_ENABLED)


@app.route('/admin/sync_schedule/<string:sync_type>', methods=['POST'])
@require_super_admin
def admin_sync_schedule_update(sync_type):
    if sync_type not in ('person', 'device', 'kace', 'ad'):
        flash('Unknown sync type.', 'error')
        return redirect(url_for('admin_sync_schedule'))
    schedule = _get_or_create_sync_schedule(sync_type)
    schedule.enabled = bool(request.form.get('enabled'))
    interval_hours = request.form.get('interval_hours', type=int)
    if interval_hours in SYNC_SCHEDULE_INTERVALS:
        schedule.interval_hours = interval_hours
    _log_activity('scheduled_sync_edit',
                   f'{"Enabled" if schedule.enabled else "Disabled"} scheduled {sync_type} sync ({SYNC_SCHEDULE_INTERVALS.get(schedule.interval_hours, schedule.interval_hours)}).')
    db.session.commit()
    flash('Schedule saved.', 'success')
    return redirect(url_for('admin_sync_schedule'))
