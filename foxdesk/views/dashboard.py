"""Pages: dashboard."""
import os
from flask import render_template
from foxdesk.core import EMAIL_ENABLED, GOOGLE_LOANER_AUTO_DISABLE_ENABLED, GOOGLE_SYNC_ENABLED, app, db
from foxdesk.services.features import feature_enabled, feature_switch as feature_on
from foxdesk.models import (
    ASSET_STATUSES,
    ActivityLog,
    Asset,
    AssetRegistry,
    BrandingSettings,
    DEVICE_TYPES,
    LoanerCheckout,
    Person,
    Repair,
    Site,
    Ticket,
)
from foxdesk.services.auth import _current_site_ids, _has_permission, login_required
from foxdesk.services.scoping import (
    _filter_registry_by_warranty,
    _scope_activity_log,
    _scope_people,
    _scope_registry,
    _scope_repairs,
    _scope_tickets,
)
from foxdesk.services.assignments import _overdue_assignments
from foxdesk.services.reports import (
    SIGNIN_DEFAULT_WINDOW_DAYS,
    _dashboard_charts,
    _data_quality_checks,
    _signin_mismatches,
)
from foxdesk.services.scheduler import SYNC_SCHEDULE_INTERVALS, _get_or_create_sync_schedule


@app.route('/admin')
@login_required
def admin_panel():
    site_ids = _current_site_ids()
    registry_count = _scope_registry(AssetRegistry.query, site_ids).count()
    people_count   = _scope_people(Person.query, site_ids).count()

    # Assets with no Asset row yet are implicitly 'available' (the default status).
    status_query = db.session.query(Asset.status, db.func.count(Asset.id))
    if site_ids is not None:
        status_query = status_query.join(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    explicit_counts = dict(status_query.group_by(Asset.status).all())
    non_available_explicit = sum(v for k, v in explicit_counts.items() if k != 'available')
    status_counts = {s: explicit_counts.get(s, 0) for s in ASSET_STATUSES}
    status_counts['available'] = registry_count - non_available_explicit

    # Device-type mix (Chromebooks vs chargers vs iPads, etc.) and a direct
    # assigned/unassigned split by Asset.assigned_to_id — distinct from
    # status_counts above, which tracks the manually-set status field
    # (a device can be unassigned but still 'repair'/'lost', for instance).
    device_type_counts = None
    assigned_count = unassigned_count = None
    if _has_permission('devices'):
        type_counts = dict(
            _scope_registry(AssetRegistry.query, site_ids)
            .with_entities(AssetRegistry.device_type, db.func.count(AssetRegistry.asset_tag))
            .group_by(AssetRegistry.device_type).all()
        )
        device_type_counts = {t: type_counts.get(t, 0) for t in DEVICE_TYPES}

        assigned_count = _scope_registry(AssetRegistry.query, site_ids) \
            .join(Asset, Asset.asset_tag == AssetRegistry.asset_tag) \
            .filter(Asset.assigned_to_id.isnot(None)).count()
        unassigned_count = registry_count - assigned_count

    # Fleet-wide Google Workspace coverage — how much of the in-scope
    # registry has ever been synced, and its last-known enabled/disabled
    # split. Cheap: three small count()s off the same base join.
    google_stats = None
    if feature_enabled('google') and _has_permission('devices'):
        google_base = _scope_registry(AssetRegistry.query, site_ids) \
            .join(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
        google_stats = {
            'synced_count':   google_base.filter(Asset.google_last_sync_at.isnot(None)).count(),
            'enabled_count':  google_base.filter(Asset.google_enabled.is_(True)).count(),
            'disabled_count': google_base.filter(Asset.google_enabled.is_(False)).count(),
        }

    overdue_count = len(_overdue_assignments(site_ids))
    warranty_expiring_count = _filter_registry_by_warranty(
        _scope_registry(AssetRegistry.query, site_ids), 'expiring').count()
    open_tickets_count = None
    if _has_permission('tickets'):
        open_tickets_count = _scope_tickets(Ticket.query, site_ids) \
            .filter(Ticket.status.in_(['open', 'in_progress'])).count()

    open_repairs_count = None
    if _has_permission('repairs'):
        open_repairs_count = _scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.is_(None)).count()

    active_loaners_count = None
    if _has_permission('loaners'):
        loaner_query = LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None))
        if site_ids is not None:
            loaner_query = loaner_query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        active_loaners_count = loaner_query.count()

    recent_activity = None
    if _has_permission('admin'):
        recent_activity = _scope_activity_log(ActivityLog.query, site_ids) \
            .order_by(ActivityLog.timestamp.desc()).limit(8).all()

    # Sync status is a super-admin-only surface (same gate as /admin/sync_schedule
    # itself) — a scoped site admin can't view or change it either.
    person_schedule = None
    device_schedule = None
    kace_schedule = None
    if site_ids is None:
        if feature_on('google'):
            person_schedule = _get_or_create_sync_schedule('person')
            device_schedule = _get_or_create_sync_schedule('device')
        if feature_on('kace'):
            kace_schedule = _get_or_create_sync_schedule('kace')

    # Orphans have no site to attribute, and a per-site breakdown only makes
    # sense district-wide — both super-admin-only, along with the onboarding
    # banners below (a scoped site admin can't act on either anyway).
    orphan_count = None
    site_breakdown = None
    unassigned_devices = None
    fresh_install = None
    dev_secrets_in_use = None
    if site_ids is None:
        orphan_count = Asset.query.filter_by(is_valid=False).count()
        site_breakdown = [{
            'name': site.name,
            'registry_count': AssetRegistry.query.filter_by(site_id=site.id).count(),
            'people_count': Person.query.filter_by(site_id=site.id).count(),
        } for site in Site.query.order_by(Site.name).all()]
        unassigned_devices = AssetRegistry.query.filter(AssetRegistry.site_id.is_(None)).count()
        fresh_install = Site.query.first() is None
        # Same check that already logs a SECURITY WARNING to stdout at startup
        # (see the IS_PRODUCTION block above) — surfaced here too since that
        # log line is invisible unless someone is tailing container logs, and
        # a reused volume could already have a Site while still running on
        # dev-fallback secrets.
        dev_secrets_in_use = (
            app.secret_key == 'dev-secret-change-in-prod'
            or os.environ.get('ADMIN_PASSWORD') is None
        )

    google_loaner_autodisable_active = (
        feature_enabled('google') and feature_enabled('loaners') and GOOGLE_LOANER_AUTO_DISABLE_ENABLED
        and Site.query.filter_by(google_loaner_autodisable_enabled=True).first() is not None
    )
    branding_settings = BrandingSettings.query.get(1)
    branding_configured = bool(branding_settings and (branding_settings.primary_color_raw or branding_settings.logo_filename))

    data_quality_errors = None
    if _has_permission('devices') and feature_enabled('data_quality'):
        data_quality_errors = sum(c['count'] for c in _data_quality_checks(site_ids) if c['severity'] == 'error')

    # "Possible violators" widget — only once there's Google device data to
    # judge by, otherwise it would just be an empty card on every install.
    violators = None
    if (_has_permission('devices') and feature_enabled('signin_check')
            and Asset.query.filter(Asset.google_last_activity.isnot(None)).first()):
        mismatches = _signin_mismatches(site_ids)
        violators = {
            'high': [m for m in mismatches if m['severity'] == 'high'],
            'medium_count': sum(1 for m in mismatches if m['severity'] == 'medium'),
            'low_count': sum(1 for m in mismatches if m['severity'] == 'low'),
            'window_days': SIGNIN_DEFAULT_WINDOW_DAYS,
        }

    return render_template('admin_panel.html',
                           registry_count=registry_count,
                           orphan_count=orphan_count,
                           people_count=people_count,
                           status_counts=status_counts,
                           device_type_counts=device_type_counts,
                           assigned_count=assigned_count,
                           unassigned_count=unassigned_count,
                           google_stats=google_stats,
                           overdue_count=overdue_count,
                           warranty_expiring_count=warranty_expiring_count,
                           open_tickets_count=open_tickets_count,
                           open_repairs_count=open_repairs_count,
                           active_loaners_count=active_loaners_count,
                           recent_activity=recent_activity,
                           person_schedule=person_schedule,
                           device_schedule=device_schedule,
                           kace_schedule=kace_schedule,
                           sync_intervals=SYNC_SCHEDULE_INTERVALS,
                           site_breakdown=site_breakdown,
                           unassigned_devices=unassigned_devices,
                           fresh_install=fresh_install,
                           dev_secrets_in_use=dev_secrets_in_use,
                           email_enabled=EMAIL_ENABLED,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           google_loaner_autodisable_active=google_loaner_autodisable_active,
                           branding_configured=branding_configured,
                           charts=_dashboard_charts(site_ids) if feature_enabled('dashboard_charts') else [],
                           data_quality_errors=data_quality_errors,
                           violators=violators)
