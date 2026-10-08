"""Sessions, permissions, site scope, the activity log, and the access decorators."""
from datetime import datetime, timezone
from flask import flash, has_request_context, jsonify, redirect, request, session, url_for
from functools import wraps
from foxdesk.core import SESSION_TIMEOUT_MINUTES, db
from foxdesk.models import ActivityLog, KioskDevice, User
from foxdesk.services.features import PERMISSION_FEATURES, feature_enabled


def _admin_session_active():
    """
    True if there's a currently valid (non-expired) admin session. Clears an
    expired session as a side effect, and refreshes the sliding timeout when valid.
    """
    if not session.get('admin_logged_in'):
        return False
    last_active = session.get('last_active')
    if last_active:
        elapsed = datetime.now(timezone.utc).timestamp() - last_active
        if elapsed > SESSION_TIMEOUT_MINUTES * 60:
            # Only drop the auth keys, not the whole session — a full clear()
            # also wipes the CSRF token, which silently invalidates any login
            # form already open in another tab (or from an earlier redirect
            # here) even though that page's token was never actually used yet.
            session.pop('admin_logged_in', None)
            session.pop('last_active', None)
            return False
    session['last_active'] = datetime.now(timezone.utc).timestamp()
    session.permanent = True
    return True


def _kiosk_device_valid():
    """True if the request carries a cookie token matching an enrolled
    KioskDevice — and kiosk mode is switched on (Settings → Features)."""
    if not feature_enabled('kiosk'):
        return False
    token = request.cookies.get('kiosk_token')
    return bool(token) and KioskDevice.query.filter_by(token=token).first() is not None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        was_logged_in = session.get('admin_logged_in')
        if not _admin_session_active():
            msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                   else 'Please log in to access the admin panel.')
            flash(msg, 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def _current_user():
    """The logged-in User row, or None if this session used the legacy shared
    ADMIN_PASSWORD login (which has no associated User record)."""
    user_id = session.get('user_id')
    return User.query.get(user_id) if user_id else None


def _current_site_ids():
    """
    None = unrestricted (super admin viewing all locations, or the legacy
    shared-password login). Otherwise the list of site_ids the current
    session may see/act on. Empty list = sees nothing — e.g. a named user
    not yet assigned any site, or a kiosk enrolled without one. Only
    meaningful inside a route already gated by
    login_required/require_permission/kiosk_or_login_required, since it
    trusts the session is already valid rather than re-checking expiry.

    A super admin can narrow this to one site at a time via the switcher
    in the nav (see admin_set_active_site) — that preference lives on
    User.default_site_id (persists across logins, not just this session)
    rather than session state, so every _scope_* helper in the app
    (registry, people, tickets, repairs, loaners, activity log, dashboard,
    nav badges — anything already keying off this function) narrows
    automatically the moment it's set, with no per-route changes needed.
    """
    if session.get('admin_logged_in'):
        if session.get('is_super_admin'):
            user = _current_user()
            if user and user.default_site_id:
                return [user.default_site_id]
            return None
        user = _current_user()
        return [s.id for s in user.sites] if user else []
    token = request.cookies.get('kiosk_token')
    if token:
        device = KioskDevice.query.filter_by(token=token).first()
        return [device.site_id] if device and device.site_id else []
    return []


def _current_actor():
    """
    Resolves who's making the current request, for the activity log. Covers
    the three real 'logged in' states this app has (named User, legacy
    shared-password login, kiosk device) plus a 'system' fallback for code
    that runs with no request context (the hourly reminder background thread).
    Returns (actor_type, actor_label, actor_user_id).
    """
    if not has_request_context():
        return 'system', 'System (background job)', None
    if session.get('admin_logged_in'):
        user = _current_user()
        if user:
            return 'user', user.username, user.id
        return 'legacy_admin', 'Admin (shared login)', None
    token = request.cookies.get('kiosk_token')
    if token:
        device = KioskDevice.query.filter_by(token=token).first()
        if device:
            return 'kiosk', f'Kiosk: {device.label or device.token[:8]}', None
    return 'system', 'System (background job)', None


def _log_activity(action, summary, site_id=None, ticket_id=None):
    """
    Records an admin-side mutation. Never commits itself — call this before
    the route's own db.session.commit() so the log entry and the action it
    describes are always atomic. site_id is best-effort; leave it None for
    anything without one clear site (Users/Sites CRUD, a multi-site bulk import).
    ticket_id is set only by ticket_* actions — it's what powers the per-ticket
    History panel on the ticket detail page (a plain-text search over summary
    would be fragile; this is a real indexed FK instead).
    """
    actor_type, actor_label, actor_user_id = _current_actor()
    db.session.add(ActivityLog(
        actor_type=actor_type, actor_label=actor_label, actor_user_id=actor_user_id,
        site_id=site_id, ticket_id=ticket_id, action=action, summary=summary,
    ))


def _has_permission(perm):
    """
    session['is_admin'] is set for both the legacy ADMIN_PASSWORD login and any
    User with is_admin=True — either way, a superuser passes every check.
    Otherwise perm must match one of the current User's can_* columns.
    """
    feature_key = PERMISSION_FEATURES.get(perm)
    if feature_key and not feature_enabled(feature_key):
        return False  # a switched-off module grants nobody access, admins included
    if session.get('is_admin'):
        return True
    user = _current_user()
    if not user or not user.is_active:
        return False
    return {
        'people':  user.can_people,
        'devices': user.can_devices or user.can_devices_manage,
        'devices_manage': user.can_devices_manage,
        'loaners': user.can_loaners,
        'loaner_checkinout': user.can_loaner_checkinout or user.can_loaners,
        'checkinout': user.can_checkinout,
        'repairs': user.can_repairs,
        'tickets': user.can_tickets,
        'manage_users': user.can_manage_users,
    }.get(perm, False)


def require_permission(perm):
    """Like login_required, but also requires the given area permission
    ('people', 'devices', 'loaners', 'repairs', or 'admin' for superuser-only)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            was_logged_in = session.get('admin_logged_in')
            if not _admin_session_active():
                msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                       else 'Please log in to access the admin panel.')
                flash(msg, 'error')
                return redirect(url_for('admin_login'))
            if not _has_permission(perm):
                flash('Your account doesn\'t have permission to access that page.', 'error')
                return redirect(url_for('admin_panel'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_super_admin(f):
    """Like require_permission, but for district-wide features (managing Sites,
    the full-registry CSV replace) that even a site-scoped is_admin=True user
    shouldn't be able to touch."""
    @wraps(f)
    def decorated(*args, **kwargs):
        was_logged_in = session.get('admin_logged_in')
        if not _admin_session_active():
            msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                   else 'Please log in to access the admin panel.')
            flash(msg, 'error')
            return redirect(url_for('admin_login'))
        if not session.get('is_super_admin'):
            flash('Only a super admin can access that page.', 'error')
            return redirect(url_for('admin_panel'))
        return f(*args, **kwargs)
    return decorated


def kiosk_or_login_required(f):
    """Allows either an active admin session or an enrolled kiosk device's cookie."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _admin_session_active() or _kiosk_device_valid():
            return f(*args, **kwargs)
        flash('Please log in, or use a device enrolled in Kiosk Mode.', 'error')
        return redirect(url_for('admin_login'))
    return decorated


def api_login_required(f):
    """Like login_required, but returns a JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_session_active():
            return jsonify({'error': 'Unauthorized. Admin login required.'}), 401
        return f(*args, **kwargs)
    return decorated


def kiosk_or_api_login_required(f):
    """Like kiosk_or_login_required, but returns a JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _admin_session_active() or _kiosk_device_valid():
            return f(*args, **kwargs)
        return jsonify({'error': 'Unauthorized. Log in or use a device enrolled in Kiosk Mode.'}), 401
    return decorated


def kiosk_or_permission_required(perm):
    """Like kiosk_or_login_required, but a logged-in (non-kiosk) session also
    needs the given area permission — a kiosk device's cookie always passes,
    since kiosks are physically dedicated to this one job."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if _kiosk_device_valid():
                return f(*args, **kwargs)
            was_logged_in = session.get('admin_logged_in')
            if not _admin_session_active():
                msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                       else 'Please log in, or use a device enrolled in Kiosk Mode.')
                flash(msg, 'error')
                return redirect(url_for('admin_login'))
            if not _has_permission(perm):
                flash('Your account doesn\'t have permission to access that page.', 'error')
                return redirect(url_for('admin_panel'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def kiosk_or_api_permission_required(perm):
    """Like kiosk_or_permission_required, but returns JSON errors instead of redirecting."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if _kiosk_device_valid():
                return f(*args, **kwargs)
            if not _admin_session_active():
                return jsonify({'error': 'Unauthorized. Log in or use a device enrolled in Kiosk Mode.'}), 401
            if not _has_permission(perm):
                return jsonify({'error': 'Your account doesn\'t have permission to do that.'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def _post_login_redirect(user):
    """Where to send someone right after logging in (or when they revisit
    /admin/login already logged in) — normally the Dashboard, but honors a
    named User's own default_landing preference (set on their Edit User
    page) when they actually still have permission to see it there, so a
    stale preference from a since-revoked permission doesn't bounce them
    to an error page instead. The legacy shared-password login has no User
    row (user=None here), so it always lands on the Dashboard."""
    if user and user.default_landing == 'loaners' and _has_permission('loaners'):
        return url_for('admin_loaners')
    # Someone who can only scan devices in and out (e.g. a library aide)
    # would find nothing on the Dashboard — send them to the scanner.
    if user and _has_permission('checkinout') and not any(
            _has_permission(p) for p in ('devices', 'people', 'loaners', 'repairs', 'tickets', 'admin')):
        return url_for('checkin_page')
    return url_for('admin_panel')
