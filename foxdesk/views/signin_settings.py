"""Pages: Settings → Sign-in (Google sign-in for staff, the shared password)."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import ALLOW_SHARED_PASSWORD, app, db
from foxdesk.models import User
from foxdesk.services.auth import _log_activity, require_super_admin
from foxdesk.services.signin import (
    google_config, redirect_uri, redirect_uri_problem, save_google_settings, shared_password_allowed, signin_settings,
)


@app.route('/admin/signin')
@require_super_admin
def admin_signin():
    settings = signin_settings()
    db.session.commit()
    uri = redirect_uri(request.url_root)
    users = User.query.filter_by(is_active=True)
    return render_template('admin_signin.html', settings=settings, cfg=google_config(), redirect_uri=uri,
                           uri_problem=redirect_uri_problem(uri), origin=uri.rsplit('/auth/', 1)[0],
                           users_total=users.count(), users_with_email=users.filter(User.email.isnot(None)).count(),
                           super_admins=users.filter_by(is_super_admin=True).count(),
                           shared_allowed=shared_password_allowed(), env_override=ALLOW_SHARED_PASSWORD)


@app.route('/admin/signin/google', methods=['POST'])
@require_super_admin
def admin_signin_google():
    try:
        save_google_settings(request.form.get('client_id'), request.form.get('client_secret'), request.form.get('domains'))
        _log_activity('signin_settings', 'Saved Google sign-in settings'
                      + (' (new client secret)' if request.form.get('client_secret') else '') + '.')
        db.session.commit()
        flash('Saved. Sign out and use "Sign in with Google" to try it.', 'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_signin'))


@app.route('/admin/signin/shared_password', methods=['POST'])
@require_super_admin
def admin_signin_shared_password():
    """Turn the shared ADMIN_PASSWORD login off (or back on). Only allowed off
    while someone can still get in as a super admin with their own account."""
    settings = signin_settings()
    turn_off = request.form.get('action') == 'off'
    if turn_off and not User.query.filter_by(is_active=True, is_super_admin=True).count():
        flash('Make at least one of your own users a super admin first, so someone can still get in.', 'error')
        return redirect(url_for('admin_signin'))
    settings.shared_password_disabled = turn_off
    _log_activity('signin_settings', f'Turned the shared admin password {"off" if turn_off else "back on"}.')
    db.session.commit()
    flash('The shared admin password is ' + ('off. Everyone signs in with their own account.' if turn_off else 'on again.'),
          'success')
    return redirect(url_for('admin_signin'))
