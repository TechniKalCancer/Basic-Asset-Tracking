"""Pages: repairs."""
from flask import flash, redirect, render_template, request, url_for
from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.automation.engine import emit
from foxdesk.models import (
    AssetRegistry,
    LoanerCheckout,
    Person,
    REPAIR_OUTCOMES,
    REPAIR_STATUSES,
    Repair,
    RepairCategory,
    RepairNote,
    TicketComment,
)
from foxdesk.services.util import _parse_date, resolve_scan
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.scoping import _scope_people, _scope_repairs
from foxdesk.services.assignments import _checkout_loaner
from foxdesk.services.helpdesk import _notify_ticket_requester, _send_device_to_repair, close_repair, link_repair_to_ticket
from foxdesk.services.attachments import _attachments_for
from foxdesk.services.overdue import repair_due_date, repair_limit_date, repair_limit_problem, repair_max_days
from foxdesk.services.schedules import where_now


@app.route('/admin/assets/<string:asset_tag>/repairs/send', methods=['POST'])
@require_permission('repairs')
def admin_repair_send(asset_tag):
    repair_category_id = request.form.get('repair_category_id', type=int)
    ticket_number = request.form.get('ticket_number', '').strip() or None
    issue_description = request.form.get('issue_description', '').strip() or None
    expected_return_at = _parse_date(request.form.get('expected_return_at'))
    try:
        status, message = _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description,
                                                   expected_return_at, site_ids=_current_site_ids())
        (db.session.commit if status == 'ok' else db.session.rollback)()
        if status == 'ok':
            _emit_repair_sent(asset_tag)
        flash(message, 'success' if status == 'ok' else 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not send device to repair: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


def _emit_repair_sent(asset_tag):
    repair = Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).order_by(Repair.id.desc()).first()
    if repair:
        emit('repair.sent', repair)


@app.route('/admin/repairs/send', methods=['POST'])
@require_permission('repairs')
def admin_repair_send_quick():
    """Quick-start version for the Repairs page — takes a raw scan value
    (tag or serial) instead of requiring you to already be on the device's
    own page, same shape as the merged Loaner Check In/Out flow."""
    scan_value = request.form.get('scan_value', '').strip()
    if not scan_value:
        flash('Scan or type the device asset tag/serial.', 'error')
        return redirect(url_for('admin_repairs'))
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(url_for('admin_repairs'))

    repair_category_id = request.form.get('repair_category_id', type=int)
    ticket_number = request.form.get('ticket_number', '').strip() or None
    issue_description = request.form.get('issue_description', '').strip() or None
    expected_return_at = _parse_date(request.form.get('expected_return_at'))
    try:
        status, message = _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description,
                                                   expected_return_at, site_ids=_current_site_ids())
        (db.session.commit if status == 'ok' else db.session.rollback)()
        if status == 'ok':
            _emit_repair_sent(asset_tag)
        flash(message, 'success' if status == 'ok' else 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not send device to repair: {e}', 'error')
    return redirect(url_for('admin_repairs'))


@app.route('/admin/repairs/<int:repair_id>')
@require_permission('repairs')
def admin_repair_detail(repair_id):
    """A repair's own page — details, edit, loaner assignment, and billing
    (via the linked Ticket's charges) all together, so billing a repair
    doesn't require jumping over to the generic Ticket detail page. The
    Ticket itself (status/priority/assignment/comments) stays one click
    away via the 'View full ticket' link, for the cases that still need it."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    active_loaner = LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first()
    default_loaner_person = repair.ticket.requester if repair.ticket else None
    student = default_loaner_person or (db.session.get(Person, active_loaner.person_id)
                                        if active_loaner and active_loaner.person_id else None)
    return render_template('admin_repair_detail.html', repair=repair, repair_categories=repair_categories,
                           repair_outcomes=REPAIR_OUTCOMES, repair_statuses=REPAIR_STATUSES, active_loaner=active_loaner,
                           default_loaner_person=default_loaner_person, due=repair_due_date(repair),
                           limit=repair_limit_date(repair.sent_at), max_days=repair_max_days(), today=utcnow().date(),
                           student=student, student_now=where_now(student) if student else None,
                           attachments=_attachments_for('repair', [repair.id])[repair.id])


@app.route('/admin/repairs/<int:repair_id>/link_ticket', methods=['POST'])
@require_permission('repairs')
def admin_repair_link_ticket(repair_id):
    """Puts this repair on a different ticket (by number), e.g. the ticket
    the problem was first reported on."""
    from foxdesk.models import Ticket
    from foxdesk.services.scoping import _scope_tickets
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    number = request.form.get('ticket_id', '').strip().lstrip('#')
    ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=int(number)).first() if number.isdigit() else None
    if ticket is None:
        flash(f'No ticket #{number} found.' if number else 'Enter a ticket number.', 'error')
        return redirect(request.referrer or fallback_url)
    _, actor_label, _ = _current_actor()
    try:
        previous = link_repair_to_ticket(repair, ticket, actor_label)
        db.session.commit()
        flash(f'This repair is on ticket #{ticket.id} now.'
              + (f' Ticket #{previous.id} is still open if you want to close or merge it.' if previous and previous.is_open else ''),
              'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/update', methods=['POST'])
@require_permission('repairs')
def admin_repair_update(repair_id):
    """Moves an open repair to a new status and/or adds a note. Both land on
    the repair's timeline, and on its ticket so the help-desk history matches."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    status = request.form.get('status', '').strip() or repair.status
    body = request.form.get('note', '').strip()[:5000] or None
    if status not in REPAIR_STATUSES:
        flash('Choose a valid status.', 'error')
        return redirect(request.referrer or fallback_url)
    if repair.returned_at and status != repair.status:
        flash('This repair is closed; you can still add a note.', 'error')
        return redirect(request.referrer or fallback_url)
    changed = status != repair.status
    if not changed and not body:
        flash('Pick a new status or write a note.', 'error')
        return redirect(request.referrer or fallback_url)
    _, actor_label, _ = _current_actor()
    try:
        now = utcnow()
        db.session.add(RepairNote(repair_id=repair.id, body=body, author_label=actor_label, created_at=now,
                                  status_from=repair.status if changed else None, status_to=status if changed else None))
        summary = []
        if changed:
            summary.append(f'Repair status: {REPAIR_STATUSES[repair.status]} → {REPAIR_STATUSES[status]}.')
            repair.status, repair.status_changed_at = status, now
        if body:
            summary.append(body)
        if repair.ticket_id:
            db.session.add(TicketComment(ticket_id=repair.ticket_id, body=' '.join(summary), author_label=actor_label))
            repair.ticket.updated_at = now
        registry_row = AssetRegistry.query.filter_by(asset_tag=repair.asset_tag).first()
        _log_activity('repair_edit', f'{repair.asset_tag}: ' + ' '.join(summary)[:200],
                      site_id=registry_row.site_id if registry_row else None, ticket_id=repair.ticket_id)
        db.session.commit()
        flash(f'Status is now {REPAIR_STATUSES[status]}.' if changed else 'Note added.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update repair: {e}', 'error')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/return', methods=['POST'])
@require_permission('repairs')
def admin_repair_return(repair_id):
    """Marks an open repair returned. The outcome drives what happens to the
    device's status next: Fixed goes back into service, Could Not Repair and
    Replaced both retire this physical unit (a Replaced device's replacement
    is added separately as an ordinary new device, not automated here).
    Redirects back to wherever the form was submitted from (device page,
    Repairs list or the ticket), so it works from every entry point. Its
    ticket is resolved along with it (close_repair)."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    outcome = request.form.get('outcome', '')
    if outcome not in REPAIR_OUTCOMES:
        flash('Choose a valid outcome.', 'error')
        return redirect(request.referrer or fallback_url)

    notes = request.form.get('notes', '').strip() or None
    _, actor_label, _ = _current_actor()
    try:
        resolved_from, open_loaner = close_repair(repair, outcome, notes, actor_label)
        db.session.commit()
        emit('repair.returned', repair)
        msg = f'{repair.asset_tag} marked returned ({REPAIR_OUTCOMES[outcome]}).'
        if resolved_from:
            _notify_ticket_requester(repair.ticket, 'ticket_resolved')
            emit('ticket.status_changed', repair.ticket, old_status=resolved_from)
            msg += f' Ticket #{repair.ticket_id} is resolved too.'
        if open_loaner:
            msg += f' Note: loaner {open_loaner.asset_tag} is still checked out to {open_loaner.person_name} — check it in once it\'s back.'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update repair: {e}', 'error')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/edit', methods=['POST'])
@require_permission('repairs')
def admin_repair_edit(repair_id):
    """Corrects an open repair's tracking details after the fact (wrong
    category, wrong RMA ticket #, an updated expected-return date) without
    having to close it out and re-send. Only open repairs are editable —
    a closed repair's record is historical."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    if repair.returned_at:
        flash('This repair has already been closed out and can no longer be edited.', 'error')
        return redirect(request.referrer or fallback_url)

    issue_description = request.form.get('issue_description', '').strip()
    if not issue_description:
        flash('Describe the issue — this field is required.', 'error')
        return redirect(request.referrer or fallback_url)

    expected_return_at = _parse_date(request.form.get('expected_return_at'))
    problem = repair_limit_problem(repair.sent_at, expected_return_at)
    if problem:
        flash(problem, 'error')
        return redirect(request.referrer or fallback_url)
    repair.repair_category_id = request.form.get('repair_category_id', type=int) or None
    repair.ticket_number = request.form.get('ticket_number', '').strip() or None
    repair.issue_description = issue_description
    repair.expected_return_at = expected_return_at or repair_limit_date(repair.sent_at)
    registry_row = AssetRegistry.query.filter_by(asset_tag=repair.asset_tag).first()
    _log_activity('repair_edit', f'Updated repair details for {repair.asset_tag}.',
                   site_id=registry_row.site_id if registry_row else None, ticket_id=repair.ticket_id)
    db.session.commit()
    flash('Repair updated.', 'success')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/assign_loaner', methods=['POST'])
@require_permission('repairs')
def admin_repair_assign_loaner(repair_id):
    """Checks out a loaner (from the loaner pool) to cover someone whose own
    device is at repair.asset_tag while it's out for repair — a thin wrapper
    over the same _checkout_loaner() the main Loaners page uses, just with
    repair_id set so it's tracked back to this repair and shows up flagged
    on the loaner pool (see admin_loaners())."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_ticket_detail', ticket_id=repair.ticket_id) if repair.ticket_id \
        else url_for('admin_repairs')
    if repair.returned_at:
        flash('This repair has already been closed out.', 'error')
        return redirect(request.referrer or fallback_url)
    if LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first():
        flash('This repair already has a loaner checked out.', 'error')
        return redirect(request.referrer or fallback_url)

    person_id = request.form.get('person_id', '').strip()
    scan_value = request.form.get('loaner_asset_tag', '').strip()
    due_date = _parse_date(request.form.get('due_date'))
    person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
    if not person or not scan_value:
        flash('Choose both a person and a loaner asset tag.', 'error')
        return redirect(request.referrer or fallback_url)
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(request.referrer or fallback_url)

    status, message = _checkout_loaner(asset_tag, person, due_date, site_ids, repair_id=repair.id)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(request.referrer or fallback_url)


REPAIR_SORT_COLUMNS = {
    'asset_tag': (Repair.asset_tag,),
    'category': (RepairCategory.name,),
    'ticket_number': (Repair.ticket_number,),
    'sent_at': (Repair.sent_at,),
    'expected_return_at': (Repair.expected_return_at,),
    'person': (Repair.person_name_snapshot,),
}


@app.route('/admin/repairs')
@require_permission('repairs')
def admin_repairs():
    """Fleet-wide open + recent-closed repair list."""
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'sent_at').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort not in REPAIR_SORT_COLUMNS:
        sort = 'sent_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    sort_cols = REPAIR_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]

    def _apply_search(query):
        query = query.outerjoin(RepairCategory, Repair.repair_category_id == RepairCategory.id)
        if not search:
            return query
        like = f'%{search}%'
        return query.filter(db.or_(
            Repair.asset_tag.ilike(like), RepairCategory.name.ilike(like), Repair.ticket_number.ilike(like),
            Repair.issue_description.ilike(like), Repair.person_name_snapshot.ilike(like),
        ))

    open_repairs = _apply_search(_scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.is_(None))) \
        .order_by(*order_exprs).all()
    closed_repairs = _apply_search(_scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.isnot(None))) \
        .order_by(Repair.returned_at.desc()).limit(50).all()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    open_repair_ids = [r.id for r in open_repairs]
    active_repair_loaners = {
        c.repair_id: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.repair_id.in_(open_repair_ids), LoanerCheckout.checked_in_at.is_(None))
    } if open_repair_ids else {}
    status_filter = request.args.get('status', '').strip()
    if status_filter == 'overdue':
        open_repairs = [r for r in open_repairs if repair_due_date(r) < utcnow().date()]
    elif status_filter in REPAIR_STATUSES:
        open_repairs = [r for r in open_repairs if r.status == status_filter]
    return render_template('admin_repairs.html', open_repairs=open_repairs, closed_repairs=closed_repairs,
                           repair_outcomes=REPAIR_OUTCOMES, repair_categories=repair_categories,
                           repair_statuses=REPAIR_STATUSES, status_filter=status_filter,
                           due_dates={r.id: repair_due_date(r) for r in open_repairs}, max_days=repair_max_days(),
                           active_repair_loaners=active_repair_loaners,
                           today=utcnow().date(), search=search, sort=sort, sort_dir=sort_dir)
