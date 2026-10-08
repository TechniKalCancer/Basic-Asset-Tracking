"""Pages: people."""
import csv
import io
from datetime import datetime
from decimal import Decimal
from flask import flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from foxdesk.core import GOOGLE_SYNC_ENABLED, app, db
from foxdesk.automation.engine import emit
from foxdesk.models import (
    IDENTITY_SOURCES, AssignmentHistory, CustomField, Incident, LoanerCheckout, Person, PersonIdentity, Site, Ticket,
)
from foxdesk.services.util import _parse_bool_csv
from foxdesk.services.identities import accounts_to_review_query, add_alias, link_account, unlink_account
from foxdesk.services.auth import (
    _current_site_ids,
    _log_activity,
    kiosk_or_api_login_required,
    require_permission,
)
from foxdesk.services.scoping import _person_search_filter, _scope_people, _sites_for_actor
from foxdesk.integrations.google import sync_person_from_google
from foxdesk.services.assignments import _release_person_assets
from foxdesk.services.incidents import _person_unpaid_fee_total


PEOPLE_SORT_COLUMNS = {
    'name': (Person.last_name, Person.first_name),
    'email': (Person.email,),
    'external_id': (Person.external_id,),
    'role': (Person.role,),
    'site': (Site.name,),
    'department': (Person.department,),
}


@app.route('/admin/people')
@require_permission('people')
def admin_people():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    show     = request.args.get('show', 'active')  # 'active' | 'inactive' | 'all'
    query    = _scope_people(Person.query, _current_site_ids())
    if show == 'active':
        query = query.filter(Person.is_active.is_(True))
    elif show == 'inactive':
        query = query.filter(Person.is_active.is_(False))
    search   = request.args.get('q', '').strip()
    if search:
        query = query.filter(_person_search_filter(search))
    insurance_filter = request.args.get('insurance', '').strip()
    if insurance_filter == '1':
        query = query.filter(Person.insurance_opted_in.is_(True))

    sort = request.args.get('sort', 'name').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in PEOPLE_SORT_COLUMNS:
        sort = 'name'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort == 'site':
        query = query.outerjoin(Site, Person.site_id == Site.id)
    sort_cols = PEOPLE_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    query = query.order_by(*order_exprs, Person.last_name, Person.first_name)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    site_ids = _current_site_ids()
    quick_role = request.args.get('quick_role', 'student').strip()
    quick_role = quick_role if quick_role in ('staff', 'student') else 'student'
    quick_site_id = request.args.get('quick_site_id', type=int)
    return render_template('admin_people.html', pagination=pagination, search=search, show=show,
                           insurance_filter=insurance_filter, sort=sort, sort_dir=sort_dir,
                           sites=_sites_for_actor(site_ids), quick_role=quick_role, quick_site_id=quick_site_id)


@app.route('/admin/people/search')
@kiosk_or_api_login_required
def admin_people_search():
    """
    Live search for the person-picker widget (e.g. on the assign page) — returns
    a small JSON list of matches instead of ever loading the full roster client-side,
    so this stays fast with thousands of people. Only active people are returned,
    so a graduated/withdrawn person can't accidentally be assigned a device.
    """
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    matches = _scope_people(Person.query, _current_site_ids()).filter(
        Person.is_active.is_(True),
        _person_search_filter(q),
    ).order_by(Person.last_name, Person.first_name).limit(20).all()

    return jsonify([{
        'id': p.id, 'full_name': p.full_name, 'email': p.email,
        'site': p.site.name if p.site else None,
    } for p in matches])


def _person_form_values():
    """Reads and normalizes the People create/edit form fields from the request."""
    grad_year_raw = request.form.get('grad_year', '').strip()
    return {
        'first_name':  request.form.get('first_name', '').strip(),
        'last_name':   request.form.get('last_name', '').strip(),
        'email':       request.form.get('email', '').strip().lower(),
        'role':        request.form.get('role', 'staff').strip(),
        'department':  request.form.get('department', '').strip() or None,
        'site_id':     request.form.get('site_id', type=int),
        'external_id': request.form.get('external_id', '').strip() or None,
        'grad_year':   int(grad_year_raw) if grad_year_raw.isdigit() else None,
        'insurance_opted_in': bool(request.form.get('insurance_opted_in')),
        'guardian_name':  request.form.get('guardian_name', '').strip() or None,
        'guardian_email': request.form.get('guardian_email', '').strip().lower() or None,
    }


def _validate_person_form(values, person_id=None, allowed_site_ids=None):
    """Returns an error message string, or None if the form values are valid.
    allowed_site_ids: None means unrestricted (super admin); otherwise the
    actor must pick one of their own sites — no blank/unassigned option."""
    if not values['first_name'] or not values['last_name'] or not values['email']:
        return 'First name, last name, and email are required.'
    if '@' not in values['email'] or '.' not in values['email'].split('@')[-1]:
        return 'Enter a valid email address.'
    if values.get('guardian_email') and ('@' not in values['guardian_email']
                                         or '.' not in values['guardian_email'].split('@')[-1]):
        return 'Enter a valid parent/guardian email address (or leave it blank).'
    if values['external_id']:
        dupe = Person.query.filter(Person.external_id == values['external_id'])
        if person_id:
            dupe = dupe.filter(Person.id != person_id)
        if dupe.first():
            return f'ID number "{values["external_id"]}" is already assigned to another person.'
    if allowed_site_ids is not None:
        if not values['site_id']:
            return 'Choose a site.'
        if values['site_id'] not in allowed_site_ids:
            return 'You can only assign people to your own site(s).'
    return None


@app.route('/admin/people/new', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_new():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, allowed_site_ids=site_ids)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=None, form=values,
                                    sites=_sites_for_actor(site_ids))

        try:
            person = Person(**values)
            db.session.add(person)
            _log_activity('person_add', f'Added {person.full_name}.', site_id=values.get('site_id'))
            db.session.commit()
            flash(f'Added {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add person: {e}', 'error')
            return render_template('admin_person_form.html', person=None, form=values,
                                    sites=_sites_for_actor(site_ids))

    return render_template('admin_person_form.html', person=None, form=None, sites=_sites_for_actor(site_ids))


@app.route('/admin/people/quick_add', methods=['POST'])
@require_permission('people')
def admin_people_quick_add():
    """Bulk-intake flow for entering a roster by hand without leaving the
    People list — type name/email, submit, land right back on the same page
    with the next entry's first field ready to go. Role and site are
    carried back via querystring so they stay put between entries, since a
    batch is usually all the same role/site. Reuses the same
    validation/creation logic as the full Add Person form."""
    site_ids = _current_site_ids()
    values = _person_form_values()
    sticky = {'quick_role': values['role'], 'quick_site_id': values['site_id']}
    error = _validate_person_form(values, allowed_site_ids=site_ids)
    if error:
        flash(error, 'error')
        return redirect(url_for('admin_people', **sticky))

    try:
        person = Person(**values)
        db.session.add(person)
        _log_activity('person_add', f'Quick-added {person.full_name}.', site_id=values.get('site_id'))
        db.session.commit()
        flash(f'Added {person.full_name} — ready for the next entry.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Could not add person: that email or ID number is already in use.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not add person: {e}', 'error')
    return redirect(url_for('admin_people', **sticky))


@app.route('/admin/people/<int:person_id>/edit', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_edit(person_id):
    site_ids = _current_site_ids()
    person = _scope_people(Person.query, site_ids).filter_by(id=person_id).first_or_404()
    custom_field_labels = {f.field_key: f.label for f in CustomField.query.filter_by(entity_type='person').all()}

    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, person_id=person_id, allowed_site_ids=site_ids)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=person, form=values,
                                    sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels,
                                    google_sync_enabled=GOOGLE_SYNC_ENABLED)

        try:
            for field, value in values.items():
                setattr(person, field, value)
            _log_activity('person_edit', f'Edited {person.full_name}.', site_id=person.site_id)
            db.session.commit()
            flash(f'Updated {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not update person: {e}', 'error')
            return render_template('admin_person_form.html', person=person, form=values,
                                    sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels,
                                    google_sync_enabled=GOOGLE_SYNC_ENABLED)

    return render_template('admin_person_form.html', person=person, form=None,
                           sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/people/<int:person_id>/accounts', methods=['POST'])
@require_permission('people')
def admin_person_accounts(person_id):
    """Add another email to a person, or detach one of their accounts.
    Detaching a synced account keeps it detached (see unlink_account)."""
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    action = request.form.get('action')
    try:
        if action == 'add':
            ident = add_alias(person, request.form.get('email', ''))
            _log_activity('person_account', f'Added {ident.email} as another email for {person.full_name}.',
                           site_id=person.site_id)
            flash(f'Added {ident.email}.', 'success')
        elif action == 'remove':
            ident = PersonIdentity.query.filter_by(id=request.form.get('identity_id', type=int),
                                                   person_id=person.id).first_or_404()
            label = f'{ident.source_label} account {ident.email or ident.username or ident.external_key}'
            unlink_account(ident)
            _log_activity('person_account', f'Removed {label} from {person.full_name}.', site_id=person.site_id)
            flash(f'Removed {label}.', 'success')
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_person_edit', person_id=person.id) + '#accounts')


@app.route('/admin/accounts/review')
@require_permission('people')
def admin_accounts_review():
    """Accounts a sync couldn't place on a person with certainty — usually
    an exact-name match (suggested, never auto-linked) or a brand-new
    account. Link it, create a person from it, or ignore it."""
    show_ignored = request.args.get('ignored') == '1'
    query = (PersonIdentity.query.filter(PersonIdentity.person_id.is_(None), PersonIdentity.review_status == 'ignored')
             if show_ignored else accounts_to_review_query())
    by_source = dict(query.with_entities(PersonIdentity.source, db.func.count(PersonIdentity.id))
                     .group_by(PersonIdentity.source).all())
    source = request.args.get('source')
    if source in by_source:
        query = query.filter(PersonIdentity.source == source)
    accounts = query.order_by(PersonIdentity.source, PersonIdentity.display_name, PersonIdentity.email).limit(500).all()
    return render_template('admin_accounts_review.html', accounts=accounts, show_ignored=show_ignored,
                           pending_count=accounts_to_review_query().count(), by_source=by_source,
                           source=source if source in by_source else None, source_labels=IDENTITY_SOURCES,
                           sites=_sites_for_actor(_current_site_ids()))


@app.route('/admin/accounts/<int:identity_id>/review', methods=['POST'])
@require_permission('people')
def admin_account_review_action(identity_id):
    ident = PersonIdentity.query.filter(PersonIdentity.id == identity_id,
                                        PersonIdentity.person_id.is_(None)).first_or_404()
    action = request.form.get('action')
    site_ids = _current_site_ids()
    who = ident.email or ident.username or ident.external_key
    try:
        if action == 'link':
            person = _scope_people(Person.query, site_ids).filter_by(
                id=request.form.get('person_id', type=int)).first()
            if not person:
                flash('Pick a person from the search list first.', 'error')
                return redirect(url_for('admin_accounts_review'))
            link_account(ident, person)
            _log_activity('person_account', f'Linked {ident.source_label} account {who} to {person.full_name}.',
                           site_id=person.site_id)
            flash(f'Linked {who} to {person.full_name}.', 'success')
        elif action == 'create':
            raw = ident.raw or {}
            first = (raw.get('first_name') or (ident.display_name or '').split(' ')[0]).strip()
            last = (raw.get('last_name') or ' '.join((ident.display_name or '').split(' ')[1:])).strip()
            site_id = request.form.get('site_id', type=int)
            role = request.form.get('role') if request.form.get('role') in ('staff', 'student') else raw.get('role')
            if not first or not last or not ident.email:
                flash('This account has no name or email to create a person from — link it instead.', 'error')
                return redirect(url_for('admin_accounts_review'))
            if site_ids is not None and site_id not in site_ids:
                flash('Choose one of your own sites.', 'error')
                return redirect(url_for('admin_accounts_review'))
            person = Person(first_name=first, last_name=last, email=ident.email, role=role or 'student',
                            site_id=site_id)
            db.session.add(person)
            db.session.flush()
            link_account(ident, person)
            _log_activity('person_add', f'Added {person.full_name} from {ident.source_label} account {who}.',
                           site_id=site_id)
            flash(f'Created {person.full_name}.', 'success')
        elif action == 'ignore':
            ident.review_status = 'ignored'
            ident.suggested_person_id = None
            _log_activity('person_account', f'Ignored {ident.source_label} account {who} (not anyone in FoxDesk).')
            flash(f'Ignored {who}. It won\'t come back on this list.', 'success')
        elif action == 'restore':
            ident.review_status = None
            flash(f'{who} is back on the review list.', 'success')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Could not save: {e}', 'error')
    return redirect(request.referrer or url_for('admin_accounts_review'))


@app.route('/admin/people/<int:person_id>/google_sync', methods=['POST'])
@require_permission('people')
def admin_person_google_sync(person_id):
    """Pulls the current org unit from Google Workspace for one person, by
    email — same cached-not-live pattern as admin_asset_google_sync()."""
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()

    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet. Set GOOGLE_SERVICE_ACCOUNT_FILE '
              'and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env to enable it.', 'info')
        return redirect(url_for('admin_person_edit', person_id=person_id))

    try:
        info = sync_person_from_google(person.email)
        person.google_org_unit = info.get('org_unit')
        person.google_last_sync_at = datetime.utcnow()
        db.session.commit()
        flash(f'Synced {person.full_name} from Google.', 'success')
    except LookupError as e:
        flash(str(e), 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Google sync failed: {e}', 'error')

    return redirect(url_for('admin_person_edit', person_id=person_id))


@app.route('/admin/people/<int:person_id>/history')
@require_permission('people')
def admin_person_history(person_id):
    """
    Full chronological history for one person — every AssignmentHistory,
    LoanerCheckout, and Incident row they're linked to, newest first. The
    reverse direction of the combined Assignment & Loaner History card on
    the asset assign page (that page answers "who's had this device";
    this one answers "what has this person had").
    """
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()

    assignments = AssignmentHistory.query.filter_by(person_id=person.id) \
        .order_by(AssignmentHistory.assigned_at.desc()).all()
    loaner_checkouts = LoanerCheckout.query.filter_by(person_id=person.id) \
        .order_by(LoanerCheckout.checked_out_at.desc()).all()
    incidents = Incident.query.filter_by(person_id=person.id) \
        .order_by(Incident.created_at.desc()).all()

    combined_history = sorted(
        [{'kind': 'assign', 'asset_tag': h.asset_tag, 'timestamp': h.assigned_at,
          'ended_at': h.unassigned_at, 'acknowledged_by': h.acknowledged_by} for h in assignments] +
        [{'kind': 'loaner', 'asset_tag': l.asset_tag, 'timestamp': l.checked_out_at,
          'ended_at': l.checked_in_at, 'acknowledged_by': l.acknowledged_by} for l in loaner_checkouts] +
        [{'kind': 'incident', 'asset_tag': i.asset_tag, 'timestamp': i.created_at,
          'description': i.description, 'fee_charged': i.fee_charged,
          'fee_amount': i.fee_amount, 'paid_at': i.paid_at} for i in incidents],
        key=lambda row: row['timestamp'], reverse=True,
    )

    return render_template('admin_person_history.html', person=person, combined_history=combined_history)


@app.route('/admin/people/<int:person_id>/delete', methods=['POST'])
@require_permission('people')
def admin_person_delete(person_id):
    """
    Permanently deletes a person record. Any assets currently assigned to them
    are unassigned first, not blocked. AssignmentHistory/Incident/LoanerCheckout/
    Ticket rows are kept (person_name/requester_name are snapshots) but their
    person_id/requester_person_id link is cleared so the foreign key doesn't
    block the delete.

    For students leaving at graduation, prefer /admin/people/graduate instead —
    it archives (is_active=False) rather than deleting, so history/incidents
    stay fully linked. Use this route for genuine data-entry mistakes.
    """
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    unpaid_total = _person_unpaid_fee_total(person.id)
    person_name, person_site_id = person.full_name, person.site_id
    try:
        unassigned = _release_person_assets(person, condition_in='Person deleted')
        AssignmentHistory.query.filter_by(person_id=person.id).update({'person_id': None})
        Incident.query.filter_by(person_id=person.id).update({'person_id': None})
        LoanerCheckout.query.filter_by(person_id=person.id).update({'person_id': None})
        Ticket.query.filter_by(requester_person_id=person.id).update({'requester_person_id': None})
        # Their extra emails go with them; synced accounts go back to Accounts to Review.
        PersonIdentity.query.filter_by(person_id=person.id, source='alias').delete()
        PersonIdentity.query.filter_by(person_id=person.id).update({'person_id': None, 'review_status': None})
        PersonIdentity.query.filter_by(suggested_person_id=person.id).update({'suggested_person_id': None})
        db.session.delete(person)
        _log_activity('person_delete', f'Deleted {person_name}.', site_id=person_site_id)
        db.session.commit()
        msg = f'Deleted {person.full_name}.'
        if unassigned:
            msg += f' Unassigned {unassigned} asset{"s" if unassigned != 1 else ""}.'
        if unpaid_total:
            msg += f' Note: they had ${unpaid_total:.2f} in unpaid fees on file.'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete person: {e}', 'error')
    return redirect(url_for('admin_people'))


@app.route('/admin/people/<int:person_id>/reactivate', methods=['POST'])
@require_permission('people')
def admin_person_reactivate(person_id):
    """Undoes an accidental graduate/archive — marks a person active again."""
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    person.is_active = True
    _log_activity('person_reactivate', f'Reactivated {person.full_name}.', site_id=person.site_id)
    db.session.commit()
    flash(f'Reactivated {person.full_name}.', 'success')
    return redirect(url_for('admin_people', show='inactive'))


@app.route('/admin/people/import', methods=['GET', 'POST'])
@require_permission('people')
def admin_people_import():
    """
    Bulk create-or-update people from a district roster CSV — the "everyone
    gets an ID number" workflow. Matches each row to an existing person by
    external_id first, falling back to email (for rows/people that don't have
    an ID number yet); if neither matches, a new person is created. Unlike the
    asset registry import, this never wipes existing rows — it's an upsert.
    """
    results = None

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_people_import'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_people_import'))

        results = []
        site_ids = _current_site_ids()
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower().replace(' ', '_') for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'first_name' not in fieldnames or 'last_name' not in fieldnames or 'email' not in fieldnames:
                flash(f'CSV must have "first_name", "last_name", and "email" columns. '
                      f'Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_people_import'))

            def clean(val):
                v = (val or '').strip()
                return v or None

            created = updated = skipped = 0
            for row in reader:
                first_name = clean(row.get('first_name'))
                last_name  = clean(row.get('last_name'))
                email      = clean(row.get('email'))
                email      = email.lower() if email else None
                external_id = clean(row.get('external_id') or row.get('staff_id') or row.get('student_id'))
                role       = clean(row.get('role'))
                role       = role.lower() if role and role.lower() in ('staff', 'student') else None
                department = clean(row.get('department'))
                site_name  = clean(row.get('site'))
                grad_year_raw = clean(row.get('grad_year') or row.get('graduation_year'))
                grad_year  = int(grad_year_raw) if grad_year_raw and grad_year_raw.isdigit() else None
                insurance_opted_in = _parse_bool_csv(row.get('insurance') or row.get('insurance_opted_in'))
                guardian_name  = clean(row.get('guardian_name') or row.get('parent_name'))
                guardian_email = clean(row.get('guardian_email') or row.get('parent_email'))
                guardian_email = guardian_email.lower() if guardian_email else None

                if not first_name or not last_name or not email:
                    skipped += 1
                    results.append({'row': email or external_id or '(blank)', 'ok': False,
                                    'message': 'Missing first_name, last_name, or email.'})
                    continue

                site_id = None
                if site_name:
                    site_row = Site.query.filter(db.func.lower(Site.name) == site_name.lower()).first()
                    if not site_row:
                        skipped += 1
                        results.append({'row': email, 'ok': False,
                                        'message': f'Unknown site "{site_name}" — add it under Sites first.'})
                        continue
                    site_id = site_row.id
                    if site_ids is not None and site_id not in site_ids:
                        skipped += 1
                        results.append({'row': email, 'ok': False,
                                        'message': f'"{site_name}" isn\'t one of your sites.'})
                        continue

                person = None
                if external_id:
                    person = Person.query.filter_by(external_id=external_id).first()
                if not person:
                    person = Person.query.filter(db.func.lower(Person.email) == email).first()

                if person and external_id and person.external_id and person.external_id != external_id:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': f'ID number conflict: {email} already has ID {person.external_id}.'})
                    continue

                if person and site_ids is not None and person.site_id not in site_ids:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': f'{email} belongs to a different site — not yours to update.'})
                    continue

                if not person and site_ids is not None and site_id is None:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': 'New person needs a "site" column value (one of your own sites).'})
                    continue

                if person:
                    person.first_name = first_name
                    person.last_name  = last_name
                    person.email      = email
                    if external_id:  person.external_id = external_id
                    if role:         person.role = role
                    if department:   person.department = department
                    if site_id:      person.site_id = site_id
                    if grad_year:    person.grad_year = grad_year
                    if insurance_opted_in is not None: person.insurance_opted_in = insurance_opted_in
                    if guardian_name:  person.guardian_name = guardian_name
                    if guardian_email: person.guardian_email = guardian_email
                    updated += 1
                    results.append({'row': email, 'ok': True, 'message': f'Updated {person.full_name}.'})
                else:
                    person = Person(
                        first_name=first_name, last_name=last_name, email=email,
                        external_id=external_id, role=role or 'staff',
                        department=department, site_id=site_id, grad_year=grad_year,
                        insurance_opted_in=bool(insurance_opted_in),
                        guardian_name=guardian_name, guardian_email=guardian_email,
                    )
                    db.session.add(person)
                    created += 1
                    results.append({'row': email, 'ok': True, 'message': f'Created {first_name} {last_name}.'})

            _log_activity('people_csv_import', f'Imported people via CSV: {created} created, {updated} updated, {skipped} skipped.')
            db.session.commit()
            flash(f'Created {created}, updated {updated}, skipped {skipped} row(s). See details below.',
                  'success' if not skipped else 'info')

        except Exception as e:
            db.session.rollback()
            flash(f'Import failed: {e}', 'error')
            return redirect(url_for('admin_people_import'))

    return render_template('admin_people_import.html', results=results)


@app.route('/admin/people/graduate', methods=['GET', 'POST'])
@require_permission('people')
def admin_people_graduate():
    """
    Bulk-removes a graduating class from the active roster in one action.
    Archives (is_active=False) rather than deletes, so assignment history and
    incident/fee records stay intact for anyone who ever had a device — it
    just stops them from showing up in People or the assign-device search.
    """
    if request.method == 'POST':
        grad_year_raw = request.form.get('grad_year', '').strip()
        if not grad_year_raw.isdigit():
            flash('Choose a valid graduation year.', 'error')
            return redirect(url_for('admin_people_graduate'))
        grad_year = int(grad_year_raw)

        students = _scope_people(Person.query, _current_site_ids()) \
            .filter_by(role='student', grad_year=grad_year, is_active=True).all()
        if not students:
            flash(f'No active students found with graduation year {grad_year}.', 'info')
            return redirect(url_for('admin_people_graduate'))

        unassigned_total = 0
        fees_total = Decimal('0')
        released = {}
        for student in students:
            released[student.id] = _release_person_assets(student, condition_in='Graduated')
            unassigned_total += released[student.id]
            fees_total += _person_unpaid_fee_total(student.id)
            student.is_active = False
        _log_activity('people_graduate', f'Graduated {len(students)} student(s), class of {grad_year}.')
        db.session.commit()
        for student in students:
            # graduation already unassigned their devices in FoxDesk, but they
            # still physically have them — rules see how many they held
            emit('person.deactivated', student, device_count=released[student.id])

        msg = (f'Graduated {len(students)} student{"s" if len(students) != 1 else ""} '
               f'(class of {grad_year}). Unassigned {unassigned_total} device'
               f'{"s" if unassigned_total != 1 else ""}.')
        if fees_total:
            msg += f' Note: ${fees_total:.2f} in unpaid fees across this class.'
        flash(msg, 'success')
        return redirect(url_for('admin_people', show='inactive'))

    site_ids = _current_site_ids()
    counts_query = db.session.query(Person.grad_year, db.func.count(Person.id)) \
        .filter(Person.role == 'student', Person.is_active.is_(True), Person.grad_year.isnot(None))
    if site_ids is not None:
        counts_query = counts_query.filter(Person.site_id.in_(site_ids))
    grad_year_counts = dict(counts_query.group_by(Person.grad_year).order_by(Person.grad_year).all())
    return render_template('admin_people_graduate.html', grad_year_counts=grad_year_counts)
