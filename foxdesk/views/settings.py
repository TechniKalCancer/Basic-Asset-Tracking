"""Pages: settings."""
import secrets
from datetime import datetime
from flask import flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import generate_password_hash
from foxdesk.core import BRANDING_UPLOAD_DIR, EMAIL_ENABLED, app, db, logger
from foxdesk.services.features import FEATURE_GROUPS, FEATURES, feature_enabled, feature_switch, set_feature
from foxdesk.models import (
    ActivityLog,
    AssetRegistry,
    CustomField,
    GoogleFieldMapping,
    GoogleOrgUnit,
    KioskDevice,
    LANDING_PAGES,
    Person,
    Site,
    Ticket,
    User,
    UserSite,
)
from foxdesk.services.util import _slugify_field_key
from foxdesk.services.branding import (
    _HEX_RE,
    _delete_branding_logo,
    _get_branding_settings,
    _save_branding_logo,
    generate_palette,
)
from foxdesk.services.emailer import (
    EMAIL_TEMPLATE_KINDS,
    EMAIL_TEMPLATE_VARIABLES,
    _SafeFormatDict,
    _email_template_sample_vars,
    _get_email_settings,
    _render_email_template,
    send_email,
)
from foxdesk.services.auth import (
    _current_actor,
    _current_site_ids,
    _current_user,
    _log_activity,
    login_required,
    require_permission,
    require_super_admin,
)
from foxdesk.services.scoping import _scope_users, _sites_for_actor
from foxdesk.services.assignments import _overdue_assignments
from foxdesk.web import _settings_sections


@app.route('/admin/custom_fields')
@require_super_admin
def admin_custom_fields():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    return render_template('admin_custom_fields.html', fields=fields, entity_type=entity_type)


@app.route('/admin/custom_fields/new', methods=['GET', 'POST'])
@require_super_admin
def admin_custom_field_new():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    if request.method == 'POST':
        entity_type = request.form.get('entity_type', entity_type)
        label = request.form.get('label', '').strip()
        field_type = request.form.get('field_type', 'text').strip()
        if field_type not in ('text', 'number', 'date', 'boolean', 'email'):
            field_type = 'text'
        if not label:
            flash('Give the field a label.', 'error')
            return render_template('admin_custom_field_form.html', entity_type=entity_type, form=request.form)

        field_key = _slugify_field_key(label)
        if CustomField.query.filter_by(entity_type=entity_type, field_key=field_key).first():
            flash(f'A field with key "{field_key}" already exists for this entity type.', 'error')
            return render_template('admin_custom_field_form.html', entity_type=entity_type, form=request.form)

        db.session.add(CustomField(entity_type=entity_type, field_key=field_key, label=label, field_type=field_type))
        _log_activity('custom_field_add', f'Added custom field "{label}" ({entity_type}).')
        db.session.commit()
        flash(f'Field "{label}" added.', 'success')
        return redirect(url_for('admin_custom_fields', entity=entity_type))

    return render_template('admin_custom_field_form.html', entity_type=entity_type, form=None)


@app.route('/admin/custom_fields/<int:field_id>/delete', methods=['POST'])
@require_super_admin
def admin_custom_field_delete(field_id):
    """Deletes a custom field's definition and any mappings that fed it —
    existing values already written into rows' custom_fields JSON are left
    alone (harmless orphaned data, not worth a bulk cleanup pass)."""
    field = CustomField.query.get_or_404(field_id)
    target = f'custom:{field.field_key}'
    GoogleFieldMapping.query.filter_by(entity_type=field.entity_type, target_field=target).delete()
    _log_activity('custom_field_delete', f'Deleted custom field "{field.label}" ({field.entity_type}).')
    db.session.delete(field)
    db.session.commit()
    flash('Field deleted.', 'success')
    return redirect(url_for('admin_custom_fields', entity=field.entity_type))


@app.route('/admin/kiosk')
@require_permission('admin')
def admin_kiosk():
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    query = KioskDevice.query
    if site_ids is not None:
        query = query.filter(KioskDevice.site_id.in_(site_ids))
    if search:
        query = query.filter(KioskDevice.label.ilike(f'%{search}%'))
    devices = query.order_by(KioskDevice.created_at.asc() if sort_dir == 'asc' else KioskDevice.created_at.desc()).all()
    token = request.cookies.get('kiosk_token')
    current_device = KioskDevice.query.filter_by(token=token).first() if token else None
    return render_template('admin_kiosk.html', devices=devices, current_device=current_device,
                           sites=_sites_for_actor(site_ids), search=search, sort_dir=sort_dir)


@app.route('/admin/kiosk/enable', methods=['POST'])
@require_permission('admin')
def admin_kiosk_enable():
    """Enrolls the device making this request (i.e. the kiosk itself) via a long-lived cookie."""
    label = request.form.get('label', '').strip() or None
    site_id = request.form.get('site_id', type=int)
    site_ids = _current_site_ids()
    if site_ids is not None and (not site_id or site_id not in site_ids):
        flash('Choose one of your own sites for this kiosk.', 'error')
        return redirect(url_for('admin_kiosk'))
    if site_ids is None and not site_id:
        flash('Choose a site for this kiosk.', 'error')
        return redirect(url_for('admin_kiosk'))
    token = secrets.token_urlsafe(32)
    try:
        db.session.add(KioskDevice(token=token, label=label, site_id=site_id))
        _log_activity('kiosk_enroll', f'Enrolled kiosk device "{label or token[:8]}".', site_id=site_id)
        db.session.commit()
        resp = redirect(url_for('admin_kiosk'))
        resp.set_cookie('kiosk_token', token, max_age=60 * 60 * 24 * 365 * 5,
                        httponly=True, samesite='Lax')
        flash('This device is now enrolled as a kiosk — Check In/Check Out will work here without logging in.', 'success')
        return resp
    except Exception as e:
        db.session.rollback()
        flash(f'Could not enroll device: {e}', 'error')
        return redirect(url_for('admin_kiosk'))


@app.route('/admin/kiosk/<int:device_id>/revoke', methods=['POST'])
@require_permission('admin')
def admin_kiosk_revoke(device_id):
    """Revocable from any admin session — doesn't require physical access to the kiosk."""
    site_ids = _current_site_ids()
    query = KioskDevice.query
    if site_ids is not None:
        query = query.filter(KioskDevice.site_id.in_(site_ids))
    device = query.filter_by(id=device_id).first_or_404()
    try:
        label = device.label or 'that device'
        device_site_id = device.site_id
        db.session.delete(device)
        _log_activity('kiosk_revoke', f'Revoked kiosk access for {label}.', site_id=device_site_id)
        db.session.commit()
        flash(f'Revoked kiosk access for {label}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not revoke device: {e}', 'error')
    return redirect(url_for('admin_kiosk'))


def _user_form_permissions(actor_is_admin):
    """
    Reads permission checkboxes from the form. is_admin is only included (and
    therefore only ever settable) when the acting session is itself is_admin —
    otherwise a manage_users-only actor could create/edit a user into a proxy
    full-admin account. Omitting the key leaves the target's existing is_admin
    value untouched on edit, and defaults to False on create.
    """
    perms = {
        'can_people':  bool(request.form.get('can_people')),
        'can_devices': bool(request.form.get('can_devices')),
        'can_devices_manage': bool(request.form.get('can_devices_manage')),
        'can_loaners': bool(request.form.get('can_loaners')),
        'can_loaner_checkinout': bool(request.form.get('can_loaner_checkinout')),
        'can_checkinout': bool(request.form.get('can_checkinout')),
        'can_repairs': bool(request.form.get('can_repairs')),
        'can_tickets': bool(request.form.get('can_tickets')),
        'can_manage_users': bool(request.form.get('can_manage_users')),
    }
    if actor_is_admin:
        perms['is_admin'] = bool(request.form.get('is_admin'))
    return perms


@app.route('/admin/users')
@require_permission('manage_users')
def admin_users():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = _scope_users(User.query, _current_site_ids())
    if search:
        query = query.filter(User.username.ilike(f'%{search}%'))
    users = query.order_by(User.username.desc() if sort_dir == 'desc' else User.username.asc()).all()
    return render_template('admin_users.html', users=users, search=search, sort_dir=sort_dir)


def _user_email_from_form(user_id=None):
    """The Google sign-in email from the form, lowercased, or None. Raises
    ValueError if it's malformed or another user already has it."""
    email = request.form.get('email', '').strip().lower() or None
    if email and ('@' not in email or ' ' in email):
        raise ValueError('Enter a valid email address.')
    if email:
        taken = User.query.filter(db.func.lower(User.email) == email, User.id != (user_id or 0)).first()
        if taken:
            raise ValueError(f'{email} is already used by "{taken.username}".')
    return email


@app.route('/admin/users/new', methods=['GET', 'POST'])
@require_permission('manage_users')
def admin_user_new():
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('admin_user_form.html', user=None, sites=sites)
        if User.query.filter(db.func.lower(User.username) == username.lower()).first():
            flash(f'Username "{username}" is already taken.', 'error')
            return render_template('admin_user_form.html', user=None, sites=sites)
        try:
            email = _user_email_from_form()
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_user_form.html', user=None, sites=sites)

        # A site-scoped admin can only grant their own sites, and only a super
        # admin can create another super admin — never trust the posted flag alone.
        wants_super_admin = site_ids is None and bool(request.form.get('is_super_admin'))
        selected_site_ids = request.form.getlist('site_ids', type=int)
        if site_ids is not None:
            selected_site_ids = [s for s in selected_site_ids if s in site_ids]

        default_landing = request.form.get('default_landing', 'dashboard').strip()
        if default_landing not in LANDING_PAGES:
            default_landing = 'dashboard'

        user = User(username=username, email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
                     is_super_admin=wants_super_admin, default_landing=default_landing,
                     **_user_form_permissions(bool(session.get('is_admin'))))
        if not wants_super_admin:
            user.sites = Site.query.filter(Site.id.in_(selected_site_ids)).all()
        db.session.add(user)
        _log_activity('user_add', f'Created user "{username}".')
        db.session.commit()
        flash(f'Created user "{username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=None, sites=sites)


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@require_permission('manage_users')
def admin_user_edit(user_id):
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    user = _scope_users(User.query, site_ids).filter_by(id=user_id).first_or_404()
    if request.method == 'POST':
        new_password = request.form.get('password', '')
        try:
            user.email = _user_email_from_form(user.id)
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_user_form.html', user=user, sites=sites)
        for field, value in _user_form_permissions(bool(session.get('is_admin'))).items():
            setattr(user, field, value)
        user.is_active = bool(request.form.get('is_active'))
        default_landing = request.form.get('default_landing', 'dashboard').strip()
        user.default_landing = default_landing if default_landing in LANDING_PAGES else 'dashboard'

        if site_ids is None:  # only a super admin can change super-admin status
            user.is_super_admin = bool(request.form.get('is_super_admin'))

        if not user.is_super_admin:
            selected_site_ids = request.form.getlist('site_ids', type=int)
            if site_ids is not None:
                selected_site_ids = [s for s in selected_site_ids if s in site_ids]
            user.sites = Site.query.filter(Site.id.in_(selected_site_ids)).all()

        if new_password:
            user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        _log_activity('user_edit', f'Edited user "{user.username}".')
        db.session.commit()
        flash(f'Updated user "{user.username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=user, sites=sites)


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@require_permission('manage_users')
def admin_user_delete(user_id):
    user = _scope_users(User.query, _current_site_ids()).filter_by(id=user_id).first_or_404()
    username = user.username
    try:
        # ActivityLog.actor_user_id and Ticket.assigned_to_user_id are plain FK
        # columns with no ON DELETE rule, so deleting a User who's ever logged
        # an action (virtually every real user) or been assigned a ticket would
        # otherwise fail with a ForeignKeyViolation. Both are best-effort links
        # only — actor_label already has the actor's name snapshotted
        # permanently — so clearing them is lossless. Cleared both BEFORE and
        # AFTER _log_activity below: an admin deleting their own account
        # resolves _log_activity's own current-actor lookup back to this same
        # user, inserting a fresh ActivityLog row that references the id
        # we're about to delete — a single clear-before pass would still
        # leave that one dangling in the self-delete case.
        ActivityLog.query.filter_by(actor_user_id=user.id).update({'actor_user_id': None})
        Ticket.query.filter_by(assigned_to_user_id=user.id).update({'assigned_to_user_id': None})
        _log_activity('user_delete', f'Deleted user "{username}".')
        ActivityLog.query.filter_by(actor_user_id=user.id).update({'actor_user_id': None})
        db.session.delete(user)
        db.session.commit()
        flash(f'Deleted user "{username}".', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete user: {e}', 'error')
    return redirect(url_for('admin_users'))


@app.route('/branding/logo/<path:filename>')
def branding_logo(filename):
    """
    Unauthenticated on purpose: the login page and kiosk-facing pages need
    to show a logo before any auth happens. filename is always one we
    generated ourselves (see _save_branding_logo's random suffix) — never
    user-supplied at save time — and send_from_directory guards against
    path traversal regardless.
    """
    return send_from_directory(BRANDING_UPLOAD_DIR, filename, max_age=86400)


@app.route('/admin/branding/preview')
@require_super_admin
def admin_branding_preview():
    """Powers the live swatch preview on the settings page as the admin
    moves the color picker — same generate_palette() the save route uses,
    just not persisted, so what they see is exactly what they'll get."""
    color = request.args.get('color', '')
    if not _HEX_RE.match(color):
        return jsonify({'error': 'invalid color'}), 400
    return jsonify(generate_palette(color))


@app.route('/admin/branding', methods=['GET', 'POST'])
@require_super_admin
def admin_branding():
    settings = _get_branding_settings()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'reset_color':
            settings.primary_color_raw = None
            settings.primary_color = None
            settings.accent_dim_color = None
            settings.accent_text_color = None
            settings.secondary_color = None
            settings.secondary_text_color = None
            settings.tertiary_color = None
            settings.tertiary_text_color = None
            _log_activity('branding_edit', 'Reset branding colors to the built-in default.')
            db.session.commit()
            flash('Colors reset to the built-in default.', 'success')
            return redirect(url_for('admin_branding'))

        if action == 'remove_logo':
            _delete_branding_logo(settings.logo_filename)
            settings.logo_filename = None
            _log_activity('branding_edit', 'Removed the district-wide default logo.')
            db.session.commit()
            flash('Logo removed.', 'success')
            return redirect(url_for('admin_branding'))

        if action == 'remove_favicon':
            _delete_branding_logo(settings.favicon_filename)
            settings.favicon_filename = None
            _log_activity('branding_edit', 'Removed the favicon.')
            db.session.commit()
            flash('Favicon removed.', 'success')
            return redirect(url_for('admin_branding'))

        app_name = request.form.get('app_name', '').strip()
        primary_color = request.form.get('primary_color', '').strip()
        logo_background = request.form.get('logo_background', '').strip()
        logo_background = logo_background if logo_background in ('light', 'dark') else None

        if not _HEX_RE.match(primary_color):
            flash('Primary color must be a valid hex color (e.g. #c8102e).', 'error')
            return render_template('admin_branding.html', settings=settings)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), 'global')
            new_favicon = _save_branding_logo(request.files.get('favicon'), 'favicon')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_branding.html', settings=settings)

        settings.app_name = app_name or None
        settings.logo_background = logo_background
        if new_logo:
            _delete_branding_logo(settings.logo_filename)
            settings.logo_filename = new_logo
        if new_favicon:
            _delete_branding_logo(settings.favicon_filename)
            settings.favicon_filename = new_favicon

        settings.primary_color_raw = primary_color
        palette = generate_palette(primary_color)
        settings.primary_color        = palette['accent']
        settings.accent_dim_color     = palette['accent_dim']
        settings.accent_text_color    = palette['accent_text']
        settings.secondary_color      = palette['secondary']
        settings.secondary_text_color = palette['secondary_text']
        settings.tertiary_color       = palette['tertiary']
        settings.tertiary_text_color  = palette['tertiary_text']

        _log_activity('branding_edit', 'Updated app branding (logo/colors).')
        db.session.commit()
        flash('Branding updated.', 'success')
        return redirect(url_for('admin_branding'))

    return render_template('admin_branding.html', settings=settings)


@app.route('/admin/emails', methods=['GET', 'POST'])
@require_super_admin
def admin_emails():
    """Lets a super admin rewrite the wording of every system email this app
    sends — loaner reminders (overdue / due-soon / no-due-date) and the
    overdue-assignment reminder — using plain {variable} placeholders.
    Blank fields fall back to the built-in default (EMAIL_TEMPLATE_KINDS)."""
    settings = _get_email_settings()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action.startswith('reset:'):
            kind = action.split(':', 1)[1]
            if kind in EMAIL_TEMPLATE_KINDS:
                setattr(settings, f'{kind}_subject', None)
                setattr(settings, f'{kind}_body', None)
                _log_activity('email_template_edit', f'Reset "{EMAIL_TEMPLATE_KINDS[kind]["label"]}" email to the built-in default.')
                db.session.commit()
                flash('Reset to the built-in default.', 'success')
            return redirect(url_for('admin_emails'))

        if action == 'notifications':
            settings.ticket_notifications_enabled = request.form.get('ticket_notifications_enabled') == 'on'
            _log_activity('email_template_edit', f'Automatic ticket emails turned '
                           f'{"on" if settings.ticket_notifications_enabled else "off"}.')
            db.session.commit()
            flash('Ticket email setting saved.', 'success')
            return redirect(url_for('admin_emails'))

        # Validate every submitted template against the sample variables
        # before saving any of them — a typo (e.g. an unclosed brace) gets
        # caught here with a clear error instead of silently breaking a
        # reminder send later.
        submitted = {}
        for kind, default in EMAIL_TEMPLATE_KINDS.items():
            if f'{kind}_subject' not in request.form and f'{kind}_body' not in request.form:
                continue  # each card on the page is its own form — leave the others alone
            subject = request.form.get(f'{kind}_subject', '').strip()
            body = request.form.get(f'{kind}_body', '').strip()
            safe_vars = _SafeFormatDict(_email_template_sample_vars())
            try:
                if subject:
                    subject.format_map(safe_vars)
                if body:
                    body.format_map(safe_vars)
            except (ValueError, IndexError) as e:
                flash(f'"{default["label"]}": invalid template syntax ({e}). Nothing was saved — fix it and try again.', 'error')
                return redirect(url_for('admin_emails'))
            submitted[kind] = (subject or None, body or None)

        for kind, (subject, body) in submitted.items():
            setattr(settings, f'{kind}_subject', subject)
            setattr(settings, f'{kind}_body', body)
        _log_activity('email_template_edit', 'Updated custom email wording.')
        db.session.commit()
        flash('Email templates updated.', 'success')
        return redirect(url_for('admin_emails'))

    safe_sample = _SafeFormatDict(_email_template_sample_vars())
    kinds = []
    for key, default in EMAIL_TEMPLATE_KINDS.items():
        current_subject = getattr(settings, f'{key}_subject') or default['subject']
        current_body = getattr(settings, f'{key}_body') or default['body']
        kinds.append({
            'key': key, 'label': default['label'],
            'subject': current_subject, 'body': current_body,
            'is_custom': bool(getattr(settings, f'{key}_subject') or getattr(settings, f'{key}_body')),
            'variables': EMAIL_TEMPLATE_VARIABLES[key],
            'preview_subject': current_subject.format_map(safe_sample),
            'preview_body': current_body.format_map(safe_sample),
        })
    return render_template('admin_emails.html', kinds=kinds, email_enabled=EMAIL_ENABLED, settings=settings,
                           sample_vars=_email_template_sample_vars())


@app.route('/admin/settings')
@login_required
def admin_settings():
    sections = _settings_sections()
    if not sections:
        flash('Your account doesn\'t have access to any settings.', 'error')
        return redirect(url_for('admin_panel'))
    return render_template('admin_settings.html', sections=sections)


@app.route('/admin/features', methods=['GET', 'POST'])
@require_super_admin
def admin_features():
    """Settings → Features: switch optional modules on or off. Every switch
    posts on every save (unchecked boxes are simply absent), so the form is
    the whole truth — no partial-update edge cases."""
    if request.method == 'POST':
        _, actor_label, _ = _current_actor()
        changed = []
        for key, spec in FEATURES.items():
            wanted = request.form.get(f'feature_{key}') == 'on'
            if feature_switch(key) != wanted:
                set_feature(key, wanted, actor_label)
                changed.append(f'{spec["label"]} {"on" if wanted else "off"}')
        if changed:
            _log_activity('features', 'Features changed: ' + '; '.join(changed) + '.')
            db.session.commit()
            flash('Saved: ' + ', '.join(changed) + '.', 'success')
        else:
            flash('Nothing changed.', 'info')
        return redirect(url_for('admin_features'))

    groups = []
    for group in FEATURE_GROUPS:
        items = []
        for key, spec in FEATURES.items():
            if spec['group'] != group:
                continue
            configured = spec['configured']() if 'configured' in spec else None
            blocked_by = [FEATURES[d]['label'] for d in spec.get('requires', ()) if not feature_enabled(d)]
            items.append(dict(key=key, label=spec['label'], desc=spec['desc'], on=feature_switch(key),
                              configured=configured, blocked_by=blocked_by,
                              requires=[FEATURES[d]['label'] for d in spec.get('requires', ())]))
        groups.append((group, items))
    return render_template('admin_features.html', groups=groups)


@app.route('/admin/set_active_site', methods=['POST'])
@require_super_admin
def admin_set_active_site():
    """Lets a super admin narrow their standing view to one site (or back
    to all) — persisted on their own User row (default_site_id), so it's a
    real default that survives logout/login, not just a per-session toggle.
    The shared legacy login has no User row to store this on, so it always
    stays unrestricted. See _current_site_ids() for where this takes effect."""
    user = _current_user()
    if not user:
        flash('The shared admin login can\'t narrow to one site — log in with a named account to use this.', 'info')
        return redirect(request.referrer or url_for('admin_panel'))
    site_id = request.form.get('site_id', type=int)
    site = Site.query.get(site_id) if site_id else None
    user.default_site_id = site.id if site else None
    db.session.commit()
    flash(f'Now viewing {site.name}.' if site else 'Now viewing all locations.', 'success')
    return redirect(request.referrer or url_for('admin_panel'))


@app.route('/admin/sites')
@require_super_admin
def admin_sites():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = Site.query
    if search:
        query = query.filter(Site.name.ilike(f'%{search}%'))
    sites = query.order_by(Site.name.desc() if sort_dir == 'desc' else Site.name.asc()).all()
    return render_template('admin_sites.html', sites=sites, search=search, sort_dir=sort_dir)


@app.route('/admin/sites/new', methods=['GET', 'POST'])
@require_super_admin
def admin_site_new():
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)
        if Site.query.filter(db.func.lower(Site.name) == name.lower()).first():
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)

        site = Site(name=name, google_loaner_autodisable_enabled=bool(request.form.get('google_loaner_autodisable_enabled')),
                    loaner_org_unit_path=request.form.get('loaner_org_unit_path', '').strip() or None)
        db.session.add(site)
        db.session.flush()  # assigns site.id, used as the logo filename prefix below

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)
        if new_logo:
            site.logo_filename = new_logo

        _log_activity('site_add', f'Added site "{name}".')
        db.session.commit()
        flash(f'Added site "{name}".', 'success')
        return redirect(url_for('admin_sites'))

    return render_template('admin_site_form.html', site=None, form=None, org_units=org_units)


@app.route('/admin/sites/<int:site_id>/edit', methods=['GET', 'POST'])
@require_super_admin
def admin_site_edit(site_id):
    site = Site.query.get_or_404(site_id)
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)
        dupe = Site.query.filter(db.func.lower(Site.name) == name.lower(), Site.id != site_id).first()
        if dupe:
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)

        site.name = name
        site.google_loaner_autodisable_enabled = bool(request.form.get('google_loaner_autodisable_enabled'))
        site.loaner_org_unit_path = request.form.get('loaner_org_unit_path', '').strip() or None
        if new_logo:
            _delete_branding_logo(site.logo_filename)
            site.logo_filename = new_logo
        elif request.form.get('remove_logo'):
            _delete_branding_logo(site.logo_filename)
            site.logo_filename = None

        _log_activity('site_edit', f'Edited site "{name}".', site_id=site.id)
        db.session.commit()
        flash(f'Updated site "{name}".', 'success')
        return redirect(url_for('admin_sites'))

    return render_template('admin_site_form.html', site=site, form=None, org_units=org_units)


@app.route('/admin/sites/<int:site_id>/delete', methods=['POST'])
@require_super_admin
def admin_site_delete(site_id):
    site = Site.query.get_or_404(site_id)
    in_use = (
        Person.query.filter_by(site_id=site.id).first()
        or AssetRegistry.query.filter_by(site_id=site.id).first()
        or KioskDevice.query.filter_by(site_id=site.id).first()
    )
    if in_use:
        flash(f'Can\'t delete "{site.name}" — it still has people, devices, or kiosks assigned to it.', 'error')
        return redirect(url_for('admin_sites'))
    try:
        site_name = site.name
        site_logo = site.logo_filename
        UserSite.query.filter_by(site_id=site.id).delete()
        # Ticket/ActivityLog/GoogleOrgUnit.site_id are all nullable, best-effort
        # tags (not blocked by the in_use check above, unlike Person/AssetRegistry/
        # KioskDevice) — clear them so a site with any ticket/log/org-unit history
        # doesn't hit a ForeignKeyViolation on delete.
        Ticket.query.filter_by(site_id=site.id).update({'site_id': None})
        ActivityLog.query.filter_by(site_id=site.id).update({'site_id': None})
        GoogleOrgUnit.query.filter_by(site_id=site.id).update({'site_id': None})
        User.query.filter_by(default_site_id=site.id).update({'default_site_id': None})
        db.session.delete(site)
        _log_activity('site_delete', f'Deleted site "{site_name}".')
        db.session.commit()
        _delete_branding_logo(site_logo)
        flash(f'Deleted site "{site.name}".', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete site: {e}', 'error')
    return redirect(url_for('admin_sites'))


@app.route('/admin/reminders')
@require_permission('admin')
def admin_reminders():
    overdue = _overdue_assignments(_current_site_ids())
    search = request.args.get('q', '').strip()
    if search:
        needle = search.lower()
        overdue = [r for r in overdue if needle in r.asset_tag.lower() or needle in (r.person_name or '').lower()]
    today = datetime.utcnow().date()
    return render_template('admin_reminders.html', overdue=overdue, today=today,
                           email_enabled=EMAIL_ENABLED, search=search)


@app.route('/admin/reminders/send', methods=['POST'])
@require_permission('admin')
def admin_reminders_send():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_reminders'))

    overdue = _overdue_assignments(_current_site_ids())
    sent, failed, skipped = 0, 0, 0

    for row in overdue:
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        days_overdue = (datetime.utcnow().date() - row.due_date).days
        subject, body = _render_email_template('assignment_overdue', {
            'first_name': person.first_name, 'full_name': person.full_name, 'asset_tag': row.asset_tag,
            'due_date': row.due_date.strftime('%Y-%m-%d'),
            'days_overdue': str(days_overdue), 'days_overdue_plural': 's' if days_overdue != 1 else '',
        })
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = datetime.utcnow()
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    if sent:
        _log_activity('reminders_send', f'Manually sent {sent} overdue-assignment reminder(s).')
    db.session.commit()

    msg = f'Sent {sent} reminder{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'error')
    return redirect(url_for('admin_reminders'))
