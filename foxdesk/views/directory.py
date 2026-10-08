"""Pages: Active Directory — setup, what to sync, computers (domain join) and
people disabled in AD. The sync itself is integrations/active_directory."""
from datetime import datetime, timedelta

from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import AD_BASE_DN, AD_BIND_PASSWORD, AD_BIND_USER, AD_CA_FILE, AD_SERVERS, AD_SYNC_ENABLED, app, db
from foxdesk.models import Asset, AssetRegistry, DeviceRecord, Person, PersonIdentity
from foxdesk.integrations.active_directory import (
    STALE_DAYS, DirectoryError, ad_settings, build_tree, connect, default_selection, describe_summary,
    read_directory, run_ad_sync, server_certificate, trust_certificate, verified_by_ca,
)
from foxdesk.services.auth import _current_site_ids, _log_activity, require_permission, require_super_admin
from foxdesk.services.identities import accounts_to_review_query
from foxdesk.services.scheduler import SYNC_SCHEDULE_INTERVALS, _get_or_create_sync_schedule
from foxdesk.services.scoping import _scope_people, _scope_registry

WORKGROUP_TYPES = ('laptop', 'desktop')


def _ad_counts():
    ad_people = PersonIdentity.query.filter_by(source='ad')
    ad_devices = DeviceRecord.query.filter_by(source='ad')
    stale_before = datetime.utcnow() - timedelta(days=STALE_DAYS)
    return dict(
        people_linked=ad_people.filter(PersonIdentity.person_id.isnot(None)).count(),
        people_review=accounts_to_review_query().filter(PersonIdentity.source == 'ad').count(),
        people_disabled=_disabled_people_query().count(),
        computers_linked=ad_devices.filter(DeviceRecord.registry_id.isnot(None)).count(),
        computers_unmatched=_unmatched_computers_query().count(),
        computers_stale=ad_devices.filter(DeviceRecord.enabled.is_(True), db.or_(
            DeviceRecord.last_seen_at.is_(None), DeviceRecord.last_seen_at < stale_before)).count(),
    )


def _disabled_people_query():
    """Active FoxDesk people whose AD account is disabled or gone — usually
    someone who left, and worth checking whether they still hold a device."""
    return (PersonIdentity.query.join(Person, PersonIdentity.person_id == Person.id)
            .filter(PersonIdentity.source == 'ad', PersonIdentity.enabled.is_(False), Person.is_active.is_(True)))


def _unmatched_computers_query():
    return DeviceRecord.query.filter(DeviceRecord.source == 'ad', DeviceRecord.registry_id.is_(None),
                                     DeviceRecord.review_status.is_(None))


def _render_setup(checks=None):
    settings = ad_settings()
    tree = settings.tree or []
    chosen = settings.user_containers is not None or settings.computer_containers is not None
    users_sel, computers_sel = ((settings.user_containers or [], settings.computer_containers or []) if chosen
                                else default_selection(tree))
    db.session.commit()  # ad_settings() may have created the row
    return render_template('admin_directory.html', configured=AD_SYNC_ENABLED, servers=AD_SERVERS,
                           base_dn=AD_BASE_DN, bind_user=AD_BIND_USER, password_set=bool(AD_BIND_PASSWORD),
                           ca_file=AD_CA_FILE, settings=settings, tree=tree, chosen=chosen,
                           users_sel=set(users_sel), computers_sel=set(computers_sel), checks=checks,
                           counts=_ad_counts(), schedule=_get_or_create_sync_schedule('ad'),
                           intervals=SYNC_SCHEDULE_INTERVALS, describe=describe_summary)


@app.route('/admin/directory')
@require_super_admin
def admin_directory():
    return _render_setup()


@app.route('/admin/directory/check', methods=['POST'])
@require_super_admin
def admin_directory_check():
    """Each DC: does it present a certificate, is it trusted, can the
    account log in. Nothing is saved."""
    if not AD_SYNC_ENABLED:
        flash('Set AD_SERVERS, AD_BASE_DN, AD_BIND_USER and AD_BIND_PASSWORD in .env and restart first.', 'error')
        return redirect(url_for('admin_directory'))
    settings = ad_settings()
    pins = settings.trusted_certs or {}
    checks = []
    for host in AD_SERVERS:
        check = dict(host=host, cert=None, error=None, pinned=False, pin_changed=False, ca_ok=False,
                     bind_ok=False, bind_error=None)
        try:
            check['cert'] = server_certificate(host)
        except DirectoryError as e:
            check['error'] = str(e)
            checks.append(check)
            continue
        pin = pins.get(host.lower())
        check['pinned'] = pin == check['cert']['sha256']
        check['pin_changed'] = bool(pin) and not check['pinned']
        check['ca_ok'] = not check['pinned'] and verified_by_ca(host)
        if check['pinned'] or check['ca_ok']:
            try:
                conn, _ = connect(settings, hosts=[host])
                conn.unbind()
                check['bind_ok'] = True
            except DirectoryError as e:
                check['bind_error'] = str(e)
        checks.append(check)
    return _render_setup(checks=checks)


@app.route('/admin/directory/trust', methods=['POST'])
@require_super_admin
def admin_directory_trust():
    settings = ad_settings()
    host = request.form.get('host', '')
    try:
        cert = trust_certificate(settings, host, request.form.get('sha256'))
        _log_activity('directory_settings', f'Trusted the LDAPS certificate of {host} (thumbprint {cert["sha1"]}).')
        db.session.commit()
        flash(f'Trusted {host}\'s certificate. Check the connection again to test the login.', 'success')
    except DirectoryError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_directory'))


@app.route('/admin/directory/untrust', methods=['POST'])
@require_super_admin
def admin_directory_untrust():
    settings = ad_settings()
    host = request.form.get('host', '').lower()
    pins = dict(settings.trusted_certs or {})
    if pins.pop(host, None):
        settings.trusted_certs = pins
        _log_activity('directory_settings', f'Stopped trusting the LDAPS certificate of {host}.')
        flash(f'{host}\'s certificate is no longer trusted.', 'success')
    db.session.commit()
    return redirect(url_for('admin_directory'))


@app.route('/admin/directory/refresh', methods=['POST'])
@require_super_admin
def admin_directory_refresh():
    """Re-read the OU list (with counts) for the picker without syncing."""
    settings = ad_settings()
    try:
        snapshot = read_directory(settings)
        settings.tree = build_tree(snapshot)
        settings.tree_updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Read {len(snapshot["users"])} users and {len(snapshot["computers"])} computers from AD.', 'success')
    except DirectoryError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_directory') + '#scope')


@app.route('/admin/directory/scope', methods=['POST'])
@require_super_admin
def admin_directory_scope():
    settings = ad_settings()
    known = {n['dn'] for n in settings.tree or []}
    settings.user_containers = sorted(dn for dn in request.form.getlist('users') if dn in known)
    settings.computer_containers = sorted(dn for dn in request.form.getlist('computers') if dn in known)
    _log_activity('directory_settings', f'Chose {len(settings.user_containers)} AD containers for people and '
                                        f'{len(settings.computer_containers)} for computers.')
    db.session.commit()
    flash('Saved. Run a sync to apply it.', 'success')
    return redirect(url_for('admin_directory') + '#scope')


@app.route('/admin/directory/sync', methods=['POST'])
@require_super_admin
def admin_directory_sync():
    try:
        summary = run_ad_sync()
        flash('Synced: ' + describe_summary(summary), 'success')
    except DirectoryError as e:
        db.session.rollback()
        settings = ad_settings()
        settings.last_error = str(e)
        db.session.commit()
        flash(str(e), 'error')
    return redirect(url_for('admin_directory'))


# ─── computers (domain join) ──────────────────────────────────────────────────

COMPUTER_TABS = [
    ('linked', 'Joined to AD'),
    ('unmatched', 'In AD, not in FoxDesk'),
    ('stale', f'No AD login in {STALE_DAYS} days'),
    ('workgroup', 'Not joined (Workgroup)'),
    ('ignored', 'Ignored'),
]


@app.route('/admin/directory/computers')
@require_permission('devices')
def admin_directory_computers():
    tab = request.args.get('show', 'linked')
    tab = tab if tab in dict(COMPUTER_TABS) else 'linked'
    site_ids = _current_site_ids()
    stale_before = datetime.utcnow() - timedelta(days=STALE_DAYS)

    def in_scope(query):
        # A computer not tied to a device has no site yet, so everyone with device access sees it.
        if site_ids is None:
            return query
        return (query.outerjoin(AssetRegistry, DeviceRecord.registry_id == AssetRegistry.id)
                .filter(db.or_(DeviceRecord.registry_id.is_(None), AssetRegistry.site_id.in_(site_ids))))

    ad = DeviceRecord.query.filter(DeviceRecord.source == 'ad')
    joined = db.select(DeviceRecord.registry_id).where(DeviceRecord.source == 'ad', DeviceRecord.registry_id.isnot(None))
    queries = {
        'linked': in_scope(ad.filter(DeviceRecord.registry_id.isnot(None))),
        'unmatched': _unmatched_computers_query(),
        'stale': in_scope(ad.filter(DeviceRecord.enabled.is_(True), db.or_(
            DeviceRecord.last_seen_at.is_(None), DeviceRecord.last_seen_at < stale_before))),
        'ignored': ad.filter(DeviceRecord.review_status == 'ignored'),
        'workgroup': _scope_registry(AssetRegistry.query, site_ids).filter(
            AssetRegistry.device_type.in_(WORKGROUP_TYPES), ~AssetRegistry.id.in_(joined)),
    }
    counts = {key: q.count() for key, q in queries.items()}
    if tab == 'workgroup':
        rows = queries[tab].order_by(AssetRegistry.asset_tag).limit(500).all()
    else:
        rows = queries[tab].order_by(DeviceRecord.hostname).limit(500).all()
    return render_template('admin_directory_computers.html', tab=tab, tabs=COMPUTER_TABS, counts=counts, rows=rows,
                           stale_days=STALE_DAYS, workgroup_types=WORKGROUP_TYPES)


@app.route('/admin/directory/computers/<int:record_id>', methods=['POST'])
@require_permission('devices')
def admin_directory_computer_action(record_id):
    rec = DeviceRecord.query.filter_by(id=record_id, source='ad').first_or_404()
    action = request.form.get('action')
    if action == 'link':
        tag = request.form.get('asset_tag', '').strip()
        row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter(
            db.func.lower(AssetRegistry.asset_tag) == tag.lower()).first()
        if not row:
            flash(f'No device with asset tag "{tag}".', 'error')
            return redirect(request.referrer or url_for('admin_directory_computers'))
        rec.registry_id, rec.review_status = row.id, None
        _log_activity('device_record', f'Linked AD computer {rec.hostname} to {row.asset_tag}.', site_id=row.site_id)
        flash(f'Linked {rec.hostname} to {row.asset_tag}.', 'success')
    elif action == 'ignore':
        # Also used to undo a wrong automatic match: an ignored record is never re-linked by a sync.
        rec.registry_id, rec.review_status = None, 'ignored'
        _log_activity('device_record', f'Ignored AD computer {rec.hostname} (not a FoxDesk device).')
        flash(f'Ignored {rec.hostname}.', 'success')
    elif action == 'restore':
        rec.review_status = None
        flash(f'{rec.hostname} is back on the list. The next sync will try to match it again.', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('admin_directory_computers'))


# ─── people disabled in AD ────────────────────────────────────────────────────

@app.route('/admin/directory/people')
@require_permission('people')
def admin_directory_people():
    rows = (_scope_people(_disabled_people_query(), _current_site_ids())
            .order_by(Person.last_name, Person.first_name).limit(500).all())
    held = dict(db.session.query(Asset.assigned_to_id, db.func.count(Asset.id))
                .filter(Asset.assigned_to_id.in_([r.person_id for r in rows] or [0]))
                .group_by(Asset.assigned_to_id).all())
    return render_template('admin_directory_people.html', rows=rows, held=held)
