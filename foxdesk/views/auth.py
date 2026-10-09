"""Pages: auth."""
from datetime import datetime, timezone
from flask import flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from foxdesk.core import (
    ADMIN_PASSWORD_HASH,
    MAX_ATTEMPTS,
    SESSION_TIMEOUT_MINUTES,
    _check_rate_limit,
    _login_attempts,
    _record_attempt,
    app,
    db,
)
from foxdesk.models import User
from foxdesk.services.auth import _admin_session_active, _current_user, _log_activity, _post_login_redirect
from foxdesk.services.features import feature_enabled
from foxdesk.services.signin import SigninError, finish, redirect_uri, shared_password_allowed, start


def _start_user_session(user):
    session.clear()
    session['admin_logged_in'] = True
    session['user_id'] = user.id
    session['is_admin'] = user.is_admin
    session['is_super_admin'] = user.is_super_admin
    session['last_active'] = datetime.now(timezone.utc).timestamp()
    session.permanent = True


def _login_page():
    return render_template('admin_login.html', google_signin=feature_enabled('google_signin'),
                           shared_password=shared_password_allowed())


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Redirect already-logged-in admins
    if session.get('admin_logged_in'):
        return redirect(_post_login_redirect(_current_user()))

    if request.method == 'POST':
        ip = request.remote_addr
        allowed, wait = _check_rate_limit(ip)

        if not allowed:
            flash(f'Too many failed attempts. Try again in {wait} seconds.', 'error')
            return _login_page()

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Blank username = the legacy shared ADMIN_PASSWORD login, always a
        # full superuser. A username looks up a named User account instead.
        if not username:
            if not shared_password_allowed():
                flash('The shared admin password is turned off. Sign in with your own account.', 'error')
                return _login_page()
            if check_password_hash(ADMIN_PASSWORD_HASH, password):
                session.clear()
                session['admin_logged_in'] = True
                session['is_admin'] = True
                session['is_super_admin'] = True
                session['last_active'] = datetime.now(timezone.utc).timestamp()
                session.permanent = True
                return redirect(url_for('admin_panel'))
        else:
            user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
            if user and user.is_active and check_password_hash(user.password_hash, password):
                _start_user_session(user)
                return redirect(_post_login_redirect(user))

        _record_attempt(ip)
        attempts_left = MAX_ATTEMPTS - len(_login_attempts[ip])
        flash(f'Invalid username or password. {attempts_left} attempt{"s" if attempts_left != 1 else ""} remaining.', 'error')

    return _login_page()


@app.route('/auth/google')
def auth_google():
    if not feature_enabled('google_signin'):
        flash('Google sign-in isn\'t turned on.', 'error')
        return redirect(url_for('admin_login'))
    try:
        return redirect(start(session, redirect_uri(request.url_root)))
    except SigninError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_login'))


@app.route('/auth/google/callback')
def auth_google_callback():
    if not feature_enabled('google_signin'):
        return redirect(url_for('admin_login'))
    try:
        user = finish(session, request.args)
    except SigninError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin_login'))
    _start_user_session(user)
    _log_activity('user_login', f'{user.username} signed in with Google ({user.email}).')
    db.session.commit()
    return redirect(_post_login_redirect(user))


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin_login'))


@app.route('/api/admin/session_status')
def session_status():
    """
    Returns seconds remaining in session — used by the timeout warning UI.
    Deliberately read-only (does NOT refresh last_active): this is polled
    every 15s just to render the countdown, and if polling itself extended
    the session, an open-but-idle tab would keep the session alive forever.
    """
    if not session.get('admin_logged_in'):
        return jsonify({'authenticated': False})
    last_active = session.get('last_active', 0)
    elapsed = datetime.now(timezone.utc).timestamp() - last_active
    remaining = max(0, SESSION_TIMEOUT_MINUTES * 60 - int(elapsed))
    return jsonify({'authenticated': True, 'seconds_remaining': remaining})


@app.route('/api/admin/session_extend', methods=['POST'])
def session_extend():
    """
    Explicitly resets the sliding timeout — called only by the "Stay logged
    in" button. session_status() above intentionally can't do this (see its
    docstring), so a real click needs its own mutating endpoint.
    """
    if not _admin_session_active():
        return jsonify({'authenticated': False}), 401
    return jsonify({'authenticated': True, 'seconds_remaining': SESSION_TIMEOUT_MINUTES * 60})
