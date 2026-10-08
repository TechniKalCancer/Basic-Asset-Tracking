"""Template globals, filters, navigation and the Settings hub listing."""
import os
from datetime import datetime
from flask import request, session
from markupsafe import Markup, escape
from foxdesk.core import GOOGLE_SYNC_ENABLED, KACE_SYNC_ENABLED, app
from foxdesk.services.features import feature_enabled, feature_switch
from foxdesk.models import Asset, LANDING_PAGES, Site, Ticket
from foxdesk.services.branding import _current_branding
from foxdesk.services.auth import _current_site_ids, _current_user, _has_permission
from foxdesk.services.scoping import _scope_tickets
from foxdesk.services.assignments import _overdue_assignments


# Maps a request path to the top-level nav tab it belongs to (longest-prefix
# match wins, so e.g. '/admin/registry' beats the generic '/admin' fallback).
# Used to highlight the active tab and pick which section's sub-nav to show.
NAV_SECTION_PREFIXES = [
    ('/admin/registry', 'devices'),
    ('/admin/assets', 'devices'),
    ('/admin/bulk_assign', 'devices'),
    ('/admin/bulk_print', 'devices'),
    ('/admin/audit', 'devices'),
    ('/admin/fees', 'devices'),
    ('/admin/repair_categories', 'devices'),
    ('/admin/collection', 'devices'),
    ('/asset_history', 'devices'),
    ('/api/assets', 'devices'),
    ('/admin/device_models', 'devices'),
    ('/admin/asset_number_ranges', 'devices'),
    ('/admin/orphans', 'devices'),
    ('/admin/scan_lookup', 'devices'),
    ('/admin/upload_csv', 'devices'),
    ('/admin/data_quality', 'devices'),
    ('/admin/signin_mismatches', 'devices'),
    ('/admin/labels', 'devices'),
    ('/admin/people', 'people'),
    ('/loaner_checkinout', 'loaners'),
    ('/loaner_checkout', 'loaners'),
    ('/loaner_checkin', 'loaners'),
    ('/admin/loaners', 'loaners'),
    ('/admin/repairs', 'repairs'),
    ('/admin/tickets', 'tickets'),
    ('/admin/ticket_categories', 'tickets'),
    ('/admin/automations', 'tickets'),
    ('/admin/pending_actions', 'tickets'),
    ('/submit_ticket', 'tickets'),
    ('/admin/settings', 'admin'),
    ('/admin/kiosk', 'admin'),
    ('/admin/reminders', 'admin'),
    ('/admin/activity', 'admin'),
    ('/admin/users', 'admin'),
    ('/admin/sites', 'admin'),
    ('/admin/branding', 'admin'),
    ('/admin/emails', 'admin'),
    ('/admin/google_setup', 'admin'),
    ('/admin/custom_fields', 'admin'),
    ('/admin/help', 'admin'),
    ('/admin/google_org_units', 'admin'),
    ('/admin/google_ou_push', 'admin'),
    ('/admin/google_field_mapping', 'admin'),
    ('/admin/sync_schedule', 'admin'),
    ('/admin', 'admin'),
    ('/checkin', 'devices'),
    ('/checkout', 'devices'),
    ('/report_problem', 'devices'),
]


def _active_nav_section():
    """Longest-prefix match of request.path against NAV_SECTION_PREFIXES.
    None means no top tab should be highlighted (e.g. /admin/search).
    /admin itself is the Dashboard tab — matched exactly, since every
    settings page also lives under /admin/."""
    path = request.path
    if path.rstrip('/') == '/admin':
        return 'dashboard'
    best = None
    for prefix, section in NAV_SECTION_PREFIXES:
        matches = path == prefix or (prefix != '/' and path.startswith(prefix.rstrip('/') + '/'))
        if matches and (best is None or len(prefix) > len(best[0])):
            best = (prefix, section)
    return best[1] if best else None


ICON_DIR = os.path.join(app.static_folder, 'icons')


def _available_icons():
    try:
        return {f[:-4] for f in os.listdir(ICON_DIR) if f.endswith('.svg')}
    except OSError:
        return set()


AVAILABLE_ICONS = _available_icons()  # read once at startup — restart (or redeploy) after adding icons


@app.template_global('icon')
def icon(name, extra_class=''):
    if name not in AVAILABLE_ICONS:
        return Markup('')
    return Markup(f'<span class="icon {escape(extra_class)}" aria-hidden="true" '
                  f'style="--icon:url(\'/static/icons/{escape(name)}.svg\')"></span>')


@app.context_processor
def inject_permission_helper():
    """Exposes can('people'|'devices'|'loaners'|'repairs') to every template, so
    nav links and buttons can hide themselves for users without that permission
    instead of just bouncing them back with an error after they click. Also
    exposes site-scope helpers so templates can hide site columns/filters for
    single-site users and gate Sites management to super admins.

    nav_overdue_count/nav_orphan_count/nav_open_tickets_count power the small
    badges on the nav tabs — only computed for a logged-in admin session (not
    kiosk-only visitors, who never see those tabs), and only when the relevant
    permission is held, so this doesn't add queries to every page load.

    active_section drives which top tab is highlighted and which section's
    sub-nav row renders — computed for every request (cheap, no DB query)."""
    nav_overdue_count = 0
    nav_orphan_count = 0
    nav_open_tickets_count = 0
    all_sites = []
    active_site = None
    if session.get('admin_logged_in'):
        if _has_permission('admin'):
            nav_overdue_count = len(_overdue_assignments(_current_site_ids()))
        if session.get('is_super_admin'):
            nav_orphan_count = Asset.query.filter_by(is_valid=False).count()
            all_sites = Site.query.order_by(Site.name).all()
            user = _current_user()
            active_site = user.default_site if user else None
        if _has_permission('tickets'):
            nav_open_tickets_count = _scope_tickets(Ticket.query, _current_site_ids()) \
                .filter(Ticket.status.in_(['open', 'in_progress'])).count()
    return {
        'can': _has_permission,
        'is_super_admin': lambda: bool(session.get('is_super_admin')),
        'current_site_ids': _current_site_ids,
        'nav_overdue_count': nav_overdue_count,
        'nav_orphan_count': nav_orphan_count,
        'nav_open_tickets_count': nav_open_tickets_count,
        'all_sites': all_sites,
        'active_site': active_site,
        'branding': _current_branding(),
        'active_section': _active_nav_section(),
        'google_sync_enabled': GOOGLE_SYNC_ENABLED,
        'kace_sync_enabled': KACE_SYNC_ENABLED,
        'landing_pages': LANDING_PAGES,
        'available_icons': sorted(AVAILABLE_ICONS),
    }


def _settings_sections():
    """Every admin/settings area, grouped, filtered to what the current user
    can actually open — the Admin tab's landing page. Kept as data so the
    hub page and the Admin sub-nav can't drift apart on permissions."""
    admin = _has_permission('admin')
    users = _has_permission('manage_users')
    sup = bool(session.get('is_super_admin'))
    sections = [
        ('General', [
            (sup, 'settings', 'Features', 'Turn modules on or off so people only see what your district uses.', '/admin/features'),
        ]),
        ('People & Access', [
            (users, 'users', 'Users & Permissions', 'Staff logins and what each one can see and do.', '/admin/users'),
            (sup, 'site', 'Sites', 'Schools and buildings; which devices and people belong where.', '/admin/sites'),
            (admin and feature_enabled('kiosk'), 'computer', 'Kiosk Devices', 'Enroll a shared device for check-in/out without a login.', '/admin/kiosk'),
        ]),
        ('Notifications', [
            (sup, 'email', 'Email', 'Wording for reminders, ticket updates, and parent damage notices.', '/admin/emails'),
            (admin and feature_enabled('reminders'), 'schedule', 'Overdue Reminders', 'Devices past their due date, and who to remind.', '/admin/reminders'),
        ]),
        ('Customize', [
            (sup, 'branding', 'Branding', 'Logo, app name, and colors.', '/admin/branding'),
            (sup, 'custom-fields', 'Custom Fields', 'Extra fields on devices and people.', '/admin/custom_fields'),
            (admin and feature_enabled('help'), 'help', 'Help Content', 'The FAQ and how-to guides on the Help page.', '/admin/help'),
        ]),
        ('Integrations', [
            (sup and feature_switch('google'), 'integration', 'Google Workspace', 'Connect Google Admin for Chromebook and user sync.', '/admin/google_setup'),
            (sup and feature_enabled('google'), 'custom-fields', 'Google Field Mapping', 'Which Google fields fill which app fields.', '/admin/google_field_mapping'),
            (sup and feature_enabled('google'), 'org-unit', 'Google Org Units', 'Map org units to sites and roles; push loaners to an OU.', '/admin/google_org_units'),
            (sup and feature_switch('kace'), 'integration', 'KACE', 'Connect the KACE SMA inventory.', '/admin/kace_setup'),
            (sup, 'sync', 'Scheduled Syncs', 'How often Google and KACE syncs run.', '/admin/sync_schedule'),
        ]),
        ('Records', [
            (admin, 'history', 'Activity Log', 'Who changed what, and when.', '/admin/activity'),
        ]),
    ]
    return [(title, [dict(icon=i, title=t, desc=d, url=u) for ok, i, t, d, u in items if ok])
            for title, items in sections if any(item[0] for item in items)]


@app.template_filter('ago')
def _ago_filter(value):
    """'3h ago' / '2d ago' for a naive-UTC datetime — reads faster than a
    timestamp when scanning a list for what's recent."""
    if not value:
        return '—'
    seconds = max(0, int((datetime.utcnow() - value).total_seconds()))
    if seconds < 3600:
        return f'{max(1, seconds // 60)}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'


ACTIVITY_LOG_ACTIONS = [
    'device_add', 'device_edit', 'device_delete', 'device_assign', 'device_unassign', 'device_status',
    'device_google_toggle', 'device_profile_clear',
    'automation_add', 'automation_edit', 'automation_delete',
    'automation_staged', 'automation_run', 'automation_dismissed',
    'registry_csv_import', 'registry_set_sites',
    'person_add', 'person_edit', 'person_delete', 'person_reactivate', 'people_csv_import', 'people_graduate',
    'loaner_toggle', 'loaner_label_edit', 'loaner_checkout', 'loaner_checkin', 'reminders_send',
    'incident_add', 'incident_delete', 'fee_paid', 'fee_edit',
    'repair_send', 'repair_return', 'repair_edit',
    'kiosk_enroll', 'kiosk_revoke',
    'user_add', 'user_edit', 'user_delete',
    'site_add', 'site_edit', 'site_delete',
    'ticket_add', 'ticket_edit', 'ticket_status', 'ticket_assign', 'ticket_comment',
    'ticket_charge_add', 'ticket_charge_delete',
    'ticket_category_add', 'ticket_category_edit', 'ticket_category_delete',
    'help_article_add', 'help_article_edit', 'help_article_delete',
    'device_model_add', 'device_model_edit', 'device_model_delete',
    'asset_number_range_add', 'asset_number_range_edit', 'asset_number_range_delete',
    'repair_category_add', 'repair_category_edit', 'repair_category_delete',
    'branding_edit', 'email_template_edit',
    'custom_field_add', 'custom_field_delete',
    'google_field_mapping_add', 'google_field_mapping_delete', 'google_field_sync',
    'kace_field_mapping_add', 'kace_field_mapping_delete', 'kace_field_sync',
    'org_unit_refresh', 'org_unit_classify',
    'loaner_ou_push',
    'scheduled_sync', 'scheduled_sync_edit',
]
