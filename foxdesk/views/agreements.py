"""Pages: device agreements — writing them, who has signed, paper copies,
protection plan payments, and reminders."""
import csv
import io
from datetime import timedelta

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for

from foxdesk.clock import utcnow
from foxdesk.core import APP_URL, EMAIL_ENABLED, app, db
from foxdesk.models import AGREEMENT_SIGNER_ROLES, Agreement, AgreementSignature, Person, Site
from foxdesk.services.agreements import (
    REMIND_EVERY_HOURS, AgreementError, needed_roles, plan_role, progress, record_paper, reminder_recipients,
    short_name, sign, status_for, undo_paper,
)
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.features import feature_enabled
from foxdesk.services.util import _parse_money

FILTERS = [('', 'Everyone'), ('unsigned', 'Not signed'), ('partial', 'Partly signed'), ('complete', 'Signed'),
           ('plan_unpaid', 'Plan not paid')]
PER_PAGE = 100


def _agreement_or_404(agreement_id):
    agreement = db.get_or_404(Agreement, agreement_id)
    site_ids = _current_site_ids()
    if site_ids is not None and agreement.site_id and agreement.site_id not in site_ids:
        abort(404)
    return agreement


def _form_values(form):
    """(values, problems) from the agreement form."""
    values = dict(
        title=form.get('title', '').strip()[:160], school_year=form.get('school_year', '').strip()[:20] or None,
        short_name=form.get('short_name', '').strip()[:20] or None,
        body=form.get('body', '').strip(), site_id=form.get('site_id', type=int) or None,
        require_student=form.get('require_student') == 'on', require_guardian=form.get('require_guardian') == 'on',
        plan_offered=form.get('plan_offered') == 'on', plan_name=form.get('plan_name', '').strip()[:120] or None,
        plan_price=_parse_money(form.get('plan_price')), plan_details=form.get('plan_details', '').strip() or None,
    )
    problems = []
    if not values['title']:
        problems.append('Give it a title.')
    if len(values['body']) < 20:
        problems.append('Paste in the agreement text.')
    if not (values['require_student'] or values['require_guardian']):
        problems.append('Choose who signs: the student, a parent, or both.')
    if values['plan_offered'] and not values['plan_name']:
        problems.append('Name the protection plan (e.g. "Device protection plan").')
    if values['site_id'] and not db.session.get(Site, values['site_id']):
        values['site_id'] = None
    return values, problems


def _agreement_sites():
    site_ids = _current_site_ids()
    query = Site.query.order_by(Site.name)
    return query.filter(Site.id.in_(site_ids or [-1])).all() if site_ids is not None else query.all()


@app.route('/admin/agreements')
@require_permission('people')
def admin_agreements():
    site_ids = _current_site_ids()
    agreements = Agreement.query.order_by(Agreement.is_active.desc(), Agreement.id.desc()).all()
    if site_ids is not None:
        agreements = [a for a in agreements if not a.site_id or a.site_id in site_ids]
    summaries = {a.id: progress(a, site_ids)[1] for a in agreements if a.is_active}
    return render_template('admin_agreements.html', agreements=agreements, summaries=summaries)


@app.route('/admin/agreements/new', methods=['GET', 'POST'])
@require_permission('admin')
def admin_agreement_new():
    if request.method == 'POST':
        values, problems = _form_values(request.form)
        if problems:
            for p in problems:
                flash(p, 'error')
            return render_template('admin_agreement_form.html', agreement=None, values=values, sites=_agreement_sites(), locked=False)
        _, actor_label, _ = _current_actor()
        agreement = Agreement(created_by=actor_label, **values)
        db.session.add(agreement)
        db.session.flush()
        _log_activity('agreement_add', f'Added device agreement "{agreement.title}".')
        db.session.commit()
        flash(f'"{agreement.title}" is open for signing.', 'success')
        return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))
    year = utcnow().year if utcnow().month >= 7 else utcnow().year - 1
    values = dict(title='Student device agreement', school_year=f'{year}-{str(year + 1)[-2:]}', body='',
                  require_student=True, require_guardian=True, plan_offered=False)
    return render_template('admin_agreement_form.html', agreement=None, values=values, sites=_agreement_sites(), locked=False)


@app.route('/admin/agreements/<int:agreement_id>/edit', methods=['GET', 'POST'])
@require_permission('admin')
def admin_agreement_edit(agreement_id):
    """Once anyone has signed, only the wording around it can change (title,
    plan details); the text and who signs are locked to what was signed."""
    agreement = _agreement_or_404(agreement_id)
    locked = agreement.signatures.count() > 0
    if request.method == 'POST':
        values, problems = _form_values(request.form)
        if locked:
            for key in ('body', 'require_student', 'require_guardian', 'plan_offered', 'plan_price', 'site_id'):
                values[key] = getattr(agreement, key)
            problems = [p for p in problems if 'agreement text' not in p and 'who signs' not in p]
        if problems:
            for p in problems:
                flash(p, 'error')
            return render_template('admin_agreement_form.html', agreement=agreement, values=values, sites=_agreement_sites(),
                                   locked=locked)
        for key, value in values.items():
            setattr(agreement, key, value)
        _log_activity('agreement_edit', f'Edited device agreement "{agreement.title}".')
        db.session.commit()
        flash('Saved.', 'success')
        return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))
    values = {c: getattr(agreement, c) for c in ('title', 'short_name', 'school_year', 'body', 'site_id', 'require_student',
                                                 'require_guardian', 'plan_offered', 'plan_name', 'plan_price', 'plan_details')}
    return render_template('admin_agreement_form.html', agreement=agreement, values=values, sites=_agreement_sites(), locked=locked)


@app.route('/admin/agreements/<int:agreement_id>/new_version', methods=['POST'])
@require_permission('admin')
def admin_agreement_new_version(agreement_id):
    """Copy a signed agreement so its text can change. The old one closes;
    everyone signs the new one."""
    old = _agreement_or_404(agreement_id)
    _, actor_label, _ = _current_actor()
    new = Agreement(created_by=actor_label, is_active=True, **{c: getattr(old, c) for c in (
        'title', 'short_name', 'school_year', 'body', 'site_id', 'require_student', 'require_guardian', 'plan_offered', 'plan_name',
        'plan_price', 'plan_details')})
    old.is_active = False
    db.session.add(new)
    db.session.flush()
    _log_activity('agreement_add', f'New version of device agreement "{old.title}" (#{old.id} closed, #{new.id} open).')
    db.session.commit()
    flash('New version made and the old one closed. Edit the text below; everyone signs this version.', 'success')
    return redirect(url_for('admin_agreement_edit', agreement_id=new.id))


@app.route('/admin/agreements/<int:agreement_id>/active', methods=['POST'])
@require_permission('admin')
def admin_agreement_active(agreement_id):
    agreement = _agreement_or_404(agreement_id)
    agreement.is_active = not agreement.is_active
    _log_activity('agreement_edit', f'{"Opened" if agreement.is_active else "Closed"} device agreement "{agreement.title}".')
    db.session.commit()
    flash(f'"{agreement.title}" is {"open for signing" if agreement.is_active else "closed"}.', 'success')
    return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))


def _filtered_rows(rows):
    show = request.args.get('show', '')
    q = request.args.get('q', '').strip().lower()
    site_id = request.args.get('site_id', type=int)
    grad_year = request.args.get('grad_year', type=int)
    if show == 'plan_unpaid':
        rows = [r for r in rows if r['plan_unpaid']]
    elif show == 'complete':
        rows = [r for r in rows if r['complete']]
    elif show == 'partial':
        rows = [r for r in rows if not r['complete'] and r['signatures']]
    elif show == 'unsigned':
        rows = [r for r in rows if not r['signatures']]
    if site_id:
        rows = [r for r in rows if r['person'].site_id == site_id]
    if grad_year:
        rows = [r for r in rows if r['person'].grad_year == grad_year]
    if q:
        rows = [r for r in rows if q in r['person'].full_name.lower() or q in (r['person'].email or '').lower()]
    return rows


@app.route('/admin/agreements/<int:agreement_id>')
@require_permission('people')
def admin_agreement_detail(agreement_id):
    agreement = _agreement_or_404(agreement_id)
    all_rows, counts = progress(agreement, _current_site_ids())
    rows = _filtered_rows(all_rows)
    page = max(request.args.get('page', 1, type=int), 1)
    pages = max((len(rows) + PER_PAGE - 1) // PER_PAGE, 1)
    reminders = reminder_recipients(agreement, all_rows) if agreement.is_active else {}
    wait = None
    if agreement.last_reminded_at and utcnow() - agreement.last_reminded_at < timedelta(hours=REMIND_EVERY_HOURS):
        wait = agreement.last_reminded_at + timedelta(hours=REMIND_EVERY_HOURS)
    grad_years = sorted({r['person'].grad_year for r in all_rows if r['person'].grad_year})
    return render_template('admin_agreement_detail.html', agreement=agreement, rows=rows[(page - 1) * PER_PAGE:page * PER_PAGE],
                           total_rows=len(rows), page=page, pages=pages, counts=counts, filters=FILTERS,
                           show=request.args.get('show', ''), q=request.args.get('q', ''), roles=AGREEMENT_SIGNER_ROLES,
                           needed=needed_roles(agreement), plan_role=plan_role(agreement), sites=_agreement_sites(),
                           site_id=request.args.get('site_id', type=int), grad_years=grad_years,
                           grad_year=request.args.get('grad_year', type=int),
                           reminder_count=len(reminders), remind_wait=wait, email_enabled=EMAIL_ENABLED,
                           portals_on=feature_enabled('portal') or feature_enabled('parent_portal'),
                           payments_on=feature_enabled('online_payments'))


@app.route('/admin/agreements/<int:agreement_id>/export')
@require_permission('people')
def admin_agreement_export(agreement_id):
    agreement = _agreement_or_404(agreement_id)
    rows = _filtered_rows(progress(agreement, _current_site_ids())[0])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Student', 'Email', 'School', 'Graduation year', 'Status', 'Still needs', 'Signed by', 'Signed at (UTC)',
                'Plan', 'Plan paid'])
    for r in rows:
        p, sigs = r['person'], r['signatures'].values()
        w.writerow([p.full_name, p.email, p.site.name if p.site else '', p.grad_year or '',
                    'Signed' if r['complete'] else ('Partly signed' if sigs else 'Not signed'),
                    ', '.join(AGREEMENT_SIGNER_ROLES[m] for m in r['missing']),
                    '; '.join(f'{s.signer_name} ({AGREEMENT_SIGNER_ROLES[s.signer_role]})' for s in sigs),
                    '; '.join(f'{s.signed_at:%Y-%m-%d %H:%M}' for s in sigs),
                    r['plan'].plan_choice if r['plan'] else '',
                    'yes' if r['plan'] and r['plan'].plan_paid_at else ('no' if r['plan_unpaid'] else '')])
    name = f'agreement-{agreement.id}-{(agreement.school_year or "").replace("/", "-")}.csv'
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename="{name}"'})


@app.route('/admin/agreements/<int:agreement_id>/paper/<int:person_id>', methods=['POST'])
@require_permission('people')
def admin_agreement_paper(agreement_id, person_id):
    """Record a signed paper copy brought to the office."""
    agreement = _agreement_or_404(agreement_id)
    person = db.get_or_404(Person, person_id)
    site_ids = _current_site_ids()
    if site_ids is not None and person.site_id not in site_ids:
        abort(404)
    _, actor_label, _ = _current_actor()
    try:
        sig = sign(agreement, person, 'paper', request.form.get('signer_name') or person.guardian_name or person.full_name,
                   plan_choice=request.form.get('plan_choice'), recorded_by=actor_label)
        if request.form.get('plan_paid') == 'on' and sig.plan_choice == 'enrolled' and not sig.plan_paid_at:
            db.session.flush()
            from foxdesk.services.billing import charge_for, mark_paid
            mark_paid([charge_for(sig)], request.form.get('method'), request.form.get('note'), actor_label)
        db.session.commit()
    except AgreementError as e:
        db.session.rollback()
        flash(str(e), 'error')
    else:
        flash(f'Paper copy recorded for {person.full_name}.', 'success')
    return redirect(request.referrer or url_for('admin_agreement_detail', agreement_id=agreement.id))


@app.route('/admin/agreement_signatures/<int:sig_id>/plan_paid', methods=['POST'])
@require_permission('people')
def admin_agreement_plan_paid(sig_id):
    sig = db.get_or_404(AgreementSignature, sig_id)
    site_ids = _current_site_ids()
    if site_ids is not None and sig.person.site_id not in site_ids:
        abort(404)
    _, actor_label, _ = _current_actor()
    from foxdesk.services.billing import charge_for, mark_paid
    done, receipts = mark_paid([charge_for(sig)], request.form.get('method'), request.form.get('note'), actor_label)
    db.session.commit()
    flash(f'Plan marked paid for {sig.person.full_name}.' + (f' Receipt {receipts[0].number} is ready to print.' if receipts else ''),
          'success')
    return redirect(request.referrer or url_for('admin_agreement_detail', agreement_id=sig.agreement_id))


@app.route('/admin/agreements/<int:agreement_id>/remind', methods=['POST'])
@require_permission('admin')
def admin_agreement_remind(agreement_id):
    """Email everyone who still has to sign, at most once a day."""
    from foxdesk.services.emailer import _send_email_in_background
    agreement = _agreement_or_404(agreement_id)
    if not EMAIL_ENABLED:
        flash('Email isn\'t set up, so reminders can\'t be sent.', 'error')
        return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))
    if agreement.last_reminded_at and utcnow() - agreement.last_reminded_at < timedelta(hours=REMIND_EVERY_HOURS):
        flash('Reminders already went out in the last day.', 'error')
        return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))
    rows, _ = progress(agreement, _current_site_ids())
    recipients = reminder_recipients(agreement, rows)
    base = APP_URL or request.url_root.rstrip('/')
    for email, names in recipients.items():
        who = ', '.join(sorted(set(names)))
        _send_email_in_background(email, f'Please sign: {agreement.title}',
                                  f'Hello,\n\n"{agreement.title}"{" for " + agreement.school_year if agreement.school_year else ""} '
                                  f'still needs your signature ({who}).\n\nSign in with this email address to sign it online:\n'
                                  f'{base}/my\n\nIf you already turned in a paper copy, you can ignore this email.\n')
    agreement.last_reminded_at = utcnow()
    _log_activity('reminders_send', f'Emailed {len(recipients)} reminder(s) to sign "{agreement.title}".')
    db.session.commit()
    flash(f'Reminders emailed to {len(recipients)} address(es).', 'success')
    return redirect(url_for('admin_agreement_detail', agreement_id=agreement.id))



def _scoped_person(person_id):
    person = db.get_or_404(Person, person_id)
    site_ids = _current_site_ids()
    if site_ids is not None and person.site_id not in site_ids:
        abort(404)
    return person


@app.route('/admin/agreements/<int:agreement_id>/check/<int:person_id>', methods=['POST'])
@require_permission('people')
def admin_agreement_check(agreement_id, person_id):
    """The People list / person page checkbox: tick = signed paper copy on
    file; untick = remove a paper copy recorded by mistake."""
    agreement = _agreement_or_404(agreement_id)
    person = _scoped_person(person_id)
    _, actor_label, _ = _current_actor()
    data = request.get_json(silent=True) or request.form
    wants_json = request.is_json
    try:
        if data.get('action') == 'undo':
            if not undo_paper(agreement, person, actor_label):
                raise AgreementError('Only a paper copy recorded here can be removed, and not once its plan is paid.')
        elif agreement.plan_offered and agreement.plan_price:
            raise AgreementError(f'"{agreement.title}" offers a protection plan, so record it from the agreement page.')
        else:
            record_paper(agreement, person, actor_label)
        db.session.commit()
    except AgreementError as e:
        db.session.rollback()
        if wants_json:
            return jsonify(error=str(e)), 400
        flash(str(e), 'error')
        return redirect(request.referrer or url_for('admin_person', person_id=person.id))
    st = status_for(agreement, person)
    if wants_json:
        sig = next(iter(st['signatures'].values()), None)
        return jsonify(complete=st['complete'], short_name=short_name(agreement), missing=st['missing'],
                       signed_by=sig.signer_name if sig else None, signed_at=sig.signed_at.strftime('%b %d, %Y') if sig else None)
    flash(f'{agreement.title}: {"signed" if st["complete"] else "not signed"} for {person.full_name}.', 'success')
    return redirect(request.referrer or url_for('admin_person', person_id=person.id))


@app.route('/admin/agreements/<int:agreement_id>/paper_bulk', methods=['POST'])
@require_permission('people')
def admin_agreement_paper_bulk(agreement_id):
    """Record signed paper copies for every ticked student at once (the
    stack collected at the start of the year)."""
    agreement = _agreement_or_404(agreement_id)
    site_ids = _current_site_ids()
    ids = [int(i) for i in request.form.getlist('person_id') if i.isdigit()]
    people = Person.query.filter(Person.id.in_(ids or [-1]))
    if site_ids is not None:
        people = people.filter(Person.site_id.in_(site_ids or [-1]))
    plan_choice = request.form.get('plan_choice') if agreement.plan_offered else None
    if agreement.plan_offered and plan_choice not in ('enrolled', 'declined'):
        flash('Choose whether these families enrolled in the plan.', 'error')
        return redirect(request.referrer or url_for('admin_agreement_detail', agreement_id=agreement.id))
    _, actor_label, _ = _current_actor()
    done = 0
    for person in people:
        try:
            if record_paper(agreement, person, actor_label, plan_choice=plan_choice):
                done += 1
        except AgreementError:
            continue
    db.session.commit()
    flash(f'Recorded {done} signed paper cop{"y" if done == 1 else "ies"}.', 'success' if done else 'info')
    return redirect(request.referrer or url_for('admin_agreement_detail', agreement_id=agreement.id))
