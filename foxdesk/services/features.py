"""Optional modules a district can switch on or off (Settings → Features).

Core is always on: devices, people, assignments, check in/out, the
dashboard, settings and the activity log. Everything listed in FEATURES can
be turned off, which hides it everywhere at once:

- area permissions (`can('loaners')`, `require_permission('tickets')`, …)
  report False for a module that's off — see PERMISSION_FEATURES and
  services/auth._has_permission — so every existing nav/permission check
  hides it with no template changes;
- every page that belongs to an off module returns a "turned off" page,
  by view module or endpoint (see _feature_for_endpoint), including the
  kiosk pages that skip permission checks;
- templates use `feature('key')` for the few places a permission check
  doesn't cover (dashboard cards, device-page sections, photo inputs).

An integration (Google, KACE, Active Directory) also needs its credentials in the
environment; its switch lets a district hide it even when configured.
"""
from collections import OrderedDict

from flask import g, has_request_context, render_template, request, session

from foxdesk.core import GOOGLE_SYNC_ENABLED, KACE_SYNC_ENABLED, app, db
from foxdesk.models import FeatureToggle

FEATURES = OrderedDict([
    ('loaners', dict(group='Devices', label='Loaners',
                     desc='A pool of short-term loaner devices with check out, check in, due dates and reminders.')),
    ('incidents', dict(group='Devices', label='Damage reports and fees',
                       desc='Log damage or loss against a device, charge fees, track payment, print invoices.')),
    ('guardian_notices', dict(group='Devices', label='Parent/guardian damage notices', requires=('incidents',),
                              desc='Email a damage notice to the student\'s parent or guardian.')),
    ('audit', dict(group='Devices', label='Asset audit',
                   desc='Walk a room or cart scanning devices to find what\'s missing.')),
    ('labels', dict(group='Devices', label='Label printing',
                    desc='Dymo labels and Avery sheets with barcodes.')),
    ('camera_scan', dict(group='Devices', label='Camera barcode scanning',
                         desc='A Scan button on every scan field that uses a phone or tablet camera (needs HTTPS).')),
    ('repairs', dict(group='Help desk', label='Repairs',
                     desc='Send devices out for repair or RMA and track them until they come back.')),
    ('parts', dict(group='Help desk', label='Parts inventory', requires=('repairs',),
                   desc='Parts on hand (screens, keyboards, chargers), used on repairs and tickets, with low-stock alerts.')),
    ('tickets', dict(group='Help desk', label='Tickets',
                     desc='A help-desk queue with categories, comments, charges and requester emails.')),

    ('attachments', dict(group='Help desk', label='Photos and files',
                         desc='Attach photos or PDFs to damage reports, tickets and repairs.')),
    ('kiosk', dict(group='Self-service', label='Kiosk mode',
                   desc='Enroll a shared device so students can check in/out, report a problem or submit a ticket without logging in.')),
    ('help', dict(group='Self-service', label='Help page',
                  desc='An FAQ and how-to guides you can edit.')),
    ('reminders', dict(group='Notifications', label='Overdue reminders',
                       desc='Email people whose assigned or loaned device is past its due date.')),
    ('dashboard_charts', dict(group='Reports', label='Dashboard charts',
                              desc='Tickets per week, repairs by model, damage by type, fees by site.')),
    ('data_quality', dict(group='Reports', label='Data quality checks',
                          desc='Duplicate serials, devices held by people who left, missing sites and more.')),
    ('signin_check', dict(group='Reports', label='Google sign-in check', requires=('google',),
                          desc='Flags students signing in to devices that aren\'t theirs ("Possible Violators").')),
    ('automations', dict(group='Automation', label='Automations',
                         desc='Rules that act on their own: "when a ticket is created and it mentions a screen, make it '
                              'high priority", "when a repair is out 14 days, email the team", and more.')),
    ('google', dict(group='Integrations', label='Google Workspace', configured=lambda: GOOGLE_SYNC_ENABLED,
                    desc='Sync Chromebooks and people from Google Admin, push asset tags and org units back.')),
    ('kace', dict(group='Integrations', label='KACE SMA', configured=lambda: KACE_SYNC_ENABLED,
                  desc='Sync Windows and Mac inventory from Quest KACE.')),
    ('active_directory', dict(group='Integrations', label='Active Directory', configured=lambda: _ad_configured(),
                              desc='Attach AD accounts to the people you already have and AD computers to your '
                                   'devices, and see which computers are domain-joined. Read-only.')),
    ('google_signin', dict(group='Sign-in', label='Sign in with Google', configured=lambda: _google_signin_configured(),
                           desc='Staff sign in with their school Google account, matched to their FoxDesk user by email.')),
    ('dell_warranty', dict(group='Integrations', label='Dell warranty lookup', configured=lambda: _dell_configured(),
                           desc='Fill in warranty end dates and ship dates for Dell devices from Dell\'s TechDirect API.')),
])
def _google_signin_configured():
    from foxdesk.services.signin import google_config
    return google_config()['configured']


def _dell_configured():
    from foxdesk.integrations.dell import dell_configured
    return dell_configured()


def _ad_configured():
    # Entered on the Active Directory page (or .env), so it's looked up, not a constant.
    from foxdesk.integrations.active_directory import ad_config
    return ad_config()['configured']


FEATURE_GROUPS = ['Devices', 'Help desk', 'Self-service', 'Notifications', 'Reports', 'Automation', 'Sign-in', 'Integrations']

# Area permissions that belong to a module — off module, no permission.
PERMISSION_FEATURES = {'loaners': 'loaners', 'loaner_checkinout': 'loaners', 'repairs': 'repairs', 'tickets': 'tickets'}

# Which module each page belongs to: first by endpoint (exceptions), then by
# the view module it lives in. Pages not listed are core.
ENDPOINT_FEATURES = {
    'admin_incident_notify_guardian': 'guardian_notices',
    'admin_repair_categories': None, 'admin_repair_category_new': None,   # shared by damage reports and repairs
    'admin_repair_category_edit': None, 'admin_repair_category_delete': None,
    'admin_automations': 'automations',
    'admin_pending_action_confirm': 'automations', 'admin_pending_action_dismiss': 'automations',
    'admin_asset_profile_clear': 'google',
    'submit_ticket_page': 'tickets',
    'admin_data_quality': 'data_quality',
    'admin_signin_mismatches': 'signin_check', 'admin_signin_mismatch_review': 'signin_check',
    'admin_assign_from_google': 'signin_check',
    'admin_audit': 'audit', 'admin_audit_scan': 'audit',
    'admin_bulk_print': 'labels', 'admin_avery_labels': 'labels',
    'admin_toggle_loaner': 'loaners', 'admin_update_loaner_label': 'loaners',
    'admin_repair_assign_loaner': 'loaners',
    'admin_kiosk': 'kiosk', 'admin_kiosk_enable': 'kiosk', 'admin_kiosk_revoke': 'kiosk',
    'admin_reminders': 'reminders', 'admin_reminders_send': 'reminders',
    'admin_loaners_send_reminders': 'reminders',
    'admin_asset_google_sync': 'google', 'admin_asset_google_toggle': 'google', 'admin_person_google_sync': 'google',
}
MODULE_FEATURES = {
    'foxdesk.views.loaners': 'loaners',
    'foxdesk.views.incidents': 'incidents',
    'foxdesk.views.repairs': 'repairs',
    'foxdesk.views.parts': 'parts',
    'foxdesk.views.tickets': 'tickets',
    'foxdesk.views.attachments': 'attachments',
    'foxdesk.views.help': 'help',
    'foxdesk.views.directory': 'active_directory',
    'foxdesk.views.warranty': 'dell_warranty',
    'foxdesk.views.signin_settings': 'google_signin',
    'foxdesk.views.rules': 'automations',
}


def _overrides():
    """{key: enabled} for every stored override — one query, cached for the
    rest of the request (templates ask many times per page)."""
    if has_request_context():
        cached = getattr(g, '_feature_overrides', None)
        if cached is None:
            cached = g._feature_overrides = {t.key: t.enabled for t in FeatureToggle.query.all()}
        return cached
    return {t.key: t.enabled for t in FeatureToggle.query.all()}


def feature_switch(key):
    """The district's own on/off choice, ignoring dependencies and config."""
    return _overrides().get(key, FEATURES[key].get('default', True))


def feature_enabled(key):
    """True if `key` is usable right now: switched on, everything it needs is
    on, and (for an integration) it's configured. Unknown keys are core."""
    spec = FEATURES.get(key)
    if spec is None:
        return True
    if not feature_switch(key):
        return False
    if 'configured' in spec and not spec['configured']():
        return False
    return all(feature_enabled(dep) for dep in spec.get('requires', ()))


def _feature_for_endpoint(endpoint):
    if not endpoint:
        return None
    if endpoint in ENDPOINT_FEATURES:
        return ENDPOINT_FEATURES[endpoint]
    view = app.view_functions.get(endpoint)
    module = getattr(view, '__module__', '')
    if module == 'foxdesk.views.integrations':
        return 'kace' if 'kace' in endpoint else ('google' if 'google' in endpoint else None)
    if endpoint == 'report_problem_page':
        return 'incidents'
    return MODULE_FEATURES.get(module)


def unavailable_reason(key):
    """Why `key` can't be used right now, as a sentence for the turned-off
    page — or None if it can."""
    spec = FEATURES[key]
    if not feature_switch(key):
        return f'{spec["label"]} is turned off.'
    missing = [FEATURES[d]['label'] for d in spec.get('requires', ()) if not feature_enabled(d)]
    if missing:
        return f'{spec["label"]} needs {" and ".join(missing)}, which isn\'t set up or is turned off.'
    if 'configured' in spec and not spec['configured']():
        return f'{spec["label"]} isn\'t connected yet.'
    return None


@app.before_request
def _block_disabled_features():
    key = _feature_for_endpoint(request.endpoint)
    if not key:
        return None
    if not session.get('admin_logged_in') and not request.cookies.get('kiosk_token'):
        return None  # let the page's own login check send them to the login screen first
    # An integration's own pages (setup, field mapping, ...) stay reachable
    # while it's switched on but not yet connected — that's how you connect it.
    usable = feature_switch(key) if 'configured' in FEATURES[key] else feature_enabled(key)
    if usable:
        return None
    return render_template('feature_off.html', module=FEATURES[key], module_key=key,
                           reason=unavailable_reason(key),
                           can_change=bool(session.get('is_super_admin'))), 404


# Jinja globals rather than a context processor so macros imported without
# context (`{% import '_macros.html' as macros %}`) can use them too.
# feature('x'): usable right now. feature_on('x'): the district has it
# switched on (an integration may still be waiting on credentials, and its
# setup page should stay reachable).
app.jinja_env.globals.update(feature=feature_enabled, feature_on=feature_switch)


def set_feature(key, enabled, actor_label):
    """Stores an override. Does not commit — caller's responsibility."""
    row = FeatureToggle.query.get(key)
    if row is None:
        row = FeatureToggle(key=key, enabled=enabled, updated_by=actor_label)
        db.session.add(row)
    else:
        row.enabled = enabled
        row.updated_by = actor_label
    if has_request_context():
        g.pop('_feature_overrides', None)
