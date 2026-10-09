"""Pages: tickets."""
from datetime import datetime, timedelta
from flask import flash, redirect, render_template, request, url_for
from foxdesk.core import EMAIL_ENABLED, app, db
from foxdesk.automation.engine import emit
from foxdesk.models import (
    AUTOMATION_ACTIONS,
    ActivityLog,
    AssetRegistry,
    PendingDeviceAction,
    Person,
    REPAIR_OUTCOMES,
    RepairCategory,
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    Ticket,
    TicketAutomation,
    TicketCategory,
    TicketCharge,
    TicketComment,
    Team,
    User,
)
from foxdesk.services.util import _parse_money, resolve_scan
from foxdesk.services.auth import (
    _current_actor,
    _current_site_ids,
    _current_user,
    _log_activity,
    kiosk_or_permission_required,
    require_permission,
    require_super_admin,
)
from foxdesk.services.scoping import (
    _scope_people,
    _scope_registry,
    _scope_tickets,
    _sites_for_actor,
    _ticket_assignees,
)
from foxdesk.services.helpdesk import (
    _create_ticket,
    _execute_device_automation_action,
    _notify_ticket_requester,
    _run_pending_device_action,
)
from foxdesk.services.attachments import _attachments_for, _save_attachments


@app.route('/submit_ticket', methods=['GET', 'POST'])
@kiosk_or_permission_required('checkinout')
def submit_ticket_page():
    """Student/staff self-service ticket submission — mirrors report_problem_page's
    shape (person-search + optional scan), but a ticket isn't always about a
    specific device, so an unresolved/blank scan is allowed here."""
    site_ids = _current_site_ids()
    categories = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        scan_value = request.form.get('scan_value', '').strip()

        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not category_id or not TicketCategory.query.filter_by(id=category_id, is_active=True).first():
            flash('Choose a category.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not description:
            flash('Describe the issue.', 'error')
            return redirect(url_for('submit_ticket_page'))

        asset_tag = None
        if scan_value:
            asset_tag, _ = resolve_scan(scan_value)
            if not asset_tag:
                asset_tag = scan_value  # fall back to raw value, same as report_problem_page

        try:
            ticket = _create_ticket(category_id, subject, description, person=person,
                                     asset_tag=asset_tag, site_id=person.site_id)
            photo_count = _save_attachments('ticket', ticket.id, request.files.getlist('photos'),
                                            uploaded_by=person.full_name)
            db.session.commit()
            emit('ticket.created', ticket)
            emailed = _notify_ticket_requester(ticket, 'ticket_received')
            flash(f'Thanks — your ticket #{ticket.id} has been submitted'
                  f'{f" with {photo_count} photo(s)" if photo_count else ""}.'
                  f'{" A confirmation was emailed to " + person.email + "." if emailed else ""}', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Could not submit ticket: {e}', 'error')
        return redirect(url_for('submit_ticket_page'))

    return render_template('submit_ticket.html', categories=categories)


TICKET_SORT_COLUMNS = {
    'created_at': (Ticket.created_at,),
    'subject': (Ticket.subject,),
    'category': (TicketCategory.name,),
    'status': (Ticket.status,),
    'priority': (Ticket.priority,),
    'requester': (Ticket.requester_name,),
    'assignee': (User.username,),
}


@app.route('/admin/tickets')
@require_permission('tickets')
def admin_tickets():
    site_ids = _current_site_ids()
    page = request.args.get('page', 1, type=int)
    per_page = 50
    status_filter = request.args.get('status', '').strip()
    category_filter = request.args.get('category_id', type=int)
    assignee_filter = request.args.get('assigned_to_user_id', type=int)
    team_filter = request.args.get('team_id', type=int)
    search = request.args.get('q', '').strip()
    view = request.args.get('view', '')
    me = _current_user()

    query = _scope_tickets(Ticket.query, site_ids)
    queue_counts = _queue_counts(_scope_tickets(Ticket.query, site_ids), me)
    if view in queue_counts:
        query = _queue_filter(query, view, me)
    if status_filter and status_filter in TICKET_STATUSES:
        query = query.filter(Ticket.status == status_filter)
    elif not status_filter:
        query = query.filter(Ticket.status.in_(['open', 'in_progress']))
    if category_filter:
        query = query.filter(Ticket.category_id == category_filter)
    if assignee_filter:
        query = query.filter(Ticket.assigned_to_user_id == assignee_filter)
    if team_filter:
        query = query.filter(Ticket.team_id == team_filter)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            Ticket.subject.ilike(like),
            Ticket.description.ilike(like),
            Ticket.requester_name.ilike(like),
            Ticket.asset_tag.ilike(like),
        ))

    sort = request.args.get('sort', 'created_at').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort not in TICKET_SORT_COLUMNS:
        sort = 'created_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    if sort == 'category':
        query = query.outerjoin(TicketCategory, Ticket.category_id == TicketCategory.id)
    elif sort == 'assignee':
        query = query.outerjoin(User, Ticket.assigned_to_user_id == User.id)
    sort_cols = TICKET_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    query = query.order_by(*order_exprs, Ticket.created_at.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    categories = TicketCategory.query.order_by(TicketCategory.name).all()
    assignees = _ticket_assignees(site_ids)
    return render_template('admin_tickets.html', pagination=pagination, categories=categories, assignees=assignees,
                           statuses=TICKET_STATUSES, status_filter=status_filter, search=search,
                           category_filter=category_filter, assignee_filter=assignee_filter,
                           sort=sort, sort_dir=sort_dir, view=view, queue_counts=queue_counts,
                           queues=QUEUES, teams=Team.query.order_by(Team.name).all(), team_filter=team_filter)


STALE_DAYS = 3
QUEUES = [('', 'All open'), ('mine', 'Mine'), ('my_teams', 'My teams'), ('unassigned', 'Unassigned'),
          ('stale', f'No update in {STALE_DAYS}+ days')]


def _queue_filter(query, view, me):
    """Saved views over open tickets. 'mine' and 'my_teams' need a named
    user; the shared admin login has neither, so they come back empty."""
    query = query.filter(Ticket.status.in_(['open', 'in_progress']))
    if view == 'mine':
        return query.filter(Ticket.assigned_to_user_id == (me.id if me else -1))
    if view == 'my_teams':
        return query.filter(Ticket.team_id.in_([t.id for t in me.teams] if me else [-1]))
    if view == 'unassigned':
        return query.filter(Ticket.assigned_to_user_id.is_(None), Ticket.team_id.is_(None))
    if view == 'stale':
        return query.filter(Ticket.updated_at < datetime.utcnow() - timedelta(days=STALE_DAYS))
    return query


def _queue_counts(base, me):
    return {key: _queue_filter(base, key, me).count() for key, _ in QUEUES if key}


@app.route('/admin/tickets/new', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_new():
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    categories = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        priority = request.form.get('priority', 'normal').strip()
        asset_tag = request.form.get('asset_tag', '').strip() or None
        site_id = request.form.get('site_id', type=int)

        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None

        if not category_id or not TicketCategory.query.filter_by(id=category_id, is_active=True).first():
            flash('Choose a category.', 'error')
            return render_template('admin_ticket_form.html', email_enabled=EMAIL_ENABLED, categories=categories, sites=sites, form=request.form)
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return render_template('admin_ticket_form.html', email_enabled=EMAIL_ENABLED, categories=categories, sites=sites, form=request.form)
        if not description:
            flash('Describe the issue.', 'error')
            return render_template('admin_ticket_form.html', email_enabled=EMAIL_ENABLED, categories=categories, sites=sites, form=request.form)
        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_ticket_form.html', email_enabled=EMAIL_ENABLED, categories=categories, sites=sites, form=request.form)

        try:
            ticket = _create_ticket(category_id, subject, description, person=person,
                                     asset_tag=asset_tag, site_id=site_id, priority=priority)
            _, actor_label, _ = _current_actor()
            _save_attachments('ticket', ticket.id, request.files.getlist('photos'), uploaded_by=actor_label)
            db.session.commit()
            emit('ticket.created', ticket)
            if request.form.get('notify_requester') == 'on':
                _notify_ticket_requester(ticket, 'ticket_received')
            flash('Ticket created.', 'success')
            return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not create ticket: {e}', 'error')

    return render_template('admin_ticket_form.html', email_enabled=EMAIL_ENABLED, categories=categories, sites=sites, form=None, priorities=TICKET_PRIORITIES)


@app.route('/admin/tickets/<int:ticket_id>')
@require_permission('tickets')
def admin_ticket_detail(ticket_id):
    site_ids = _current_site_ids()
    ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=ticket_id).first_or_404()
    registry_row = AssetRegistry.query.filter_by(asset_tag=ticket.asset_tag).first() if ticket.asset_tag else None
    history = ActivityLog.query.filter_by(ticket_id=ticket.id).order_by(ActivityLog.timestamp.desc()).all()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    pending_action = PendingDeviceAction.query.filter_by(ticket_id=ticket.id, status='pending').first()
    return render_template('admin_ticket_detail.html', ticket=ticket, registry_row=registry_row,
                           statuses=TICKET_STATUSES, priorities=TICKET_PRIORITIES,
                           assignees=_ticket_assignees(site_ids), history=history,
                           repair_outcomes=REPAIR_OUTCOMES, repair_categories=repair_categories,
                           pending_action=pending_action, action_labels=AUTOMATION_ACTIONS,
                           attachments=_attachments_for('ticket', [ticket.id])[ticket.id],
                           email_enabled=EMAIL_ENABLED, teams=Team.query.order_by(Team.name).all(),
                           canned_replies=_canned_for(ticket), me=_current_user())


def _canned_for(ticket):
    """The tech's own saved replies and the shared ones, ones for this
    ticket's category first, then most used."""
    from foxdesk.models import CannedReply
    me = _current_user()
    replies = CannedReply.query.filter(db.or_(CannedReply.user_id.is_(None),
                                              CannedReply.user_id == (me.id if me else -1))).all()
    return sorted(replies, key=lambda r: (r.category_id != ticket.category_id, -r.use_count, r.title.lower()))


@app.route('/admin/tickets/<int:ticket_id>/edit', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_edit(ticket_id):
    """Edits a ticket's core fields (subject, description, category, linked
    asset, site, requester) — separate from the status/priority/assignment
    mini-forms on the detail page, which already worked fine and didn't need
    touching."""
    site_ids = _current_site_ids()
    ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=ticket_id).first_or_404()
    sites = _sites_for_actor(site_ids)
    categories = TicketCategory.query.order_by(TicketCategory.name).all()

    if request.method == 'POST':
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        asset_tag = request.form.get('asset_tag', '').strip() or None
        site_id = request.form.get('site_id', type=int)
        person_id = request.form.get('person_id', '').strip()

        if not category_id or not TicketCategory.query.get(category_id):
            flash('Choose a category.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if not description:
            flash('Describe the issue.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)

        changes = []
        if ticket.category_id != category_id:
            old_name = ticket.category.name if ticket.category else 'none'
            new_name = TicketCategory.query.get(category_id).name
            changes.append(f'category: "{old_name}" → "{new_name}"')
            ticket.category_id = category_id
        if ticket.subject != subject:
            changes.append(f'subject: "{ticket.subject}" → "{subject}"')
            ticket.subject = subject
        if ticket.description != description:
            changes.append('description updated')
            ticket.description = description
        if ticket.asset_tag != asset_tag:
            changes.append(f'device: {ticket.asset_tag or "none"} → {asset_tag or "none"}')
            ticket.asset_tag = asset_tag
        if ticket.site_id != site_id:
            changes.append('site updated')
            ticket.site_id = site_id
        if person_id.isdigit():
            person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first()
            if person and ticket.requester_person_id != person.id:
                changes.append(f'requester: "{ticket.requester_name or "none"}" → "{person.full_name}"')
                ticket.requester_person_id = person.id
                ticket.requester_name = person.full_name
                ticket.requester_email = person.email

        ticket.updated_at = datetime.utcnow()
        if changes:
            _log_activity('ticket_edit', f'Edited ticket #{ticket.id} — {"; ".join(changes)}.',
                           site_id=ticket.site_id, ticket_id=ticket.id)
        db.session.commit()
        flash(f'Ticket #{ticket.id} updated.', 'success')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))

    return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)


@app.route('/admin/tickets/<int:ticket_id>/comment', methods=['POST'])
@require_permission('tickets')
def admin_ticket_comment(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    body = request.form.get('body', '').strip()
    if not body:
        flash('Comment cannot be blank.', 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    _, actor_label, _ = _current_actor()
    send_reply = request.form.get('email_requester') == 'on'
    if send_reply and not (EMAIL_ENABLED and ticket.requester_email):
        flash('Can\'t email this reply — ' + ('email isn\'t configured.' if not EMAIL_ENABLED
              else 'the ticket has no requester email.') + ' Nothing was saved.', 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    db.session.add(TicketComment(ticket_id=ticket.id, body=body, author_label=actor_label,
                                 emailed_to_requester=send_reply))
    ticket.updated_at = datetime.utcnow()
    _log_activity('ticket_comment', f'{"Replied to requester on" if send_reply else "Commented on"} ticket #{ticket.id}.',
                   site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    if send_reply:
        me = _current_user()
        emailed_body = f'{body}\n\n-- \n{me.signature}' if me and me.signature else body
        _notify_ticket_requester(ticket, 'ticket_reply', {'tech_name': actor_label, 'reply_body': emailed_body}, force=True)
        flash(f'Reply emailed to {ticket.requester_email}.', 'success')
    else:
        flash('Comment added.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/status', methods=['POST'])
@require_permission('tickets')
def admin_ticket_status(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    status = request.form.get('status', '').strip()
    priority = request.form.get('priority', '').strip()
    old_status, old_priority = ticket.status, ticket.priority
    if status and status in TICKET_STATUSES:
        ticket.status = status
        ticket.resolved_at = datetime.utcnow() if status in ('resolved', 'closed') else None
    if priority and priority in TICKET_PRIORITIES:
        ticket.priority = priority
    ticket.updated_at = datetime.utcnow()
    changes = []
    if old_status != ticket.status:
        changes.append(f'status: {old_status} → {ticket.status}')
    if old_priority != ticket.priority:
        changes.append(f'priority: {old_priority} → {ticket.priority}')
    if changes:
        _log_activity('ticket_status', f'Ticket #{ticket.id} — {"; ".join(changes)}.', site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    # Only on the transition INTO resolved/closed — resolved → closed is
    # bookkeeping, not news to the requester.
    if ticket.status in ('resolved', 'closed') and old_status not in ('resolved', 'closed'):
        _notify_ticket_requester(ticket, 'ticket_resolved')
    if old_status != ticket.status:
        emit('ticket.status_changed', ticket, old_status=old_status)
    flash(f'Ticket #{ticket.id} updated.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/assign', methods=['POST'])
@require_permission('tickets')
def admin_ticket_assign(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    def who():
        person = ticket.assigned_to.username if ticket.assigned_to_user_id and ticket.assigned_to else None
        team = ticket.team.name if ticket.team_id and ticket.team else None
        return ' / '.join(x for x in (team, person) if x) or 'nobody'
    old_label = who()
    assignee_id = request.form.get('assigned_to_user_id', type=int)
    ticket.assigned_to_user_id = assignee_id or None
    if 'team_id' in request.form:
        team_id = request.form.get('team_id', type=int)
        ticket.team_id = team_id if team_id and Team.query.get(team_id) else None
    ticket.updated_at = datetime.utcnow()
    db.session.flush()
    db.session.expire(ticket, ['assigned_to', 'team'])
    label = who()
    _log_activity('ticket_assign', f'Ticket #{ticket.id} reassigned: {old_label} → {label}.', site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash(f'Ticket #{ticket.id} assigned to {label}.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/charges', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_add(ticket_id):
    """Adds one itemized charge to a ticket. Tickets can carry several
    distinct charges over their life, so this can be called repeatedly."""
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    description = request.form.get('description', '').strip()
    amount = _parse_money(request.form.get('amount'))
    if not description:
        flash('Describe what this charge is for.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))
    if not amount:
        flash('Enter a charge amount.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))
    db.session.add(TicketCharge(ticket_id=ticket.id, description=description, amount=amount))
    ticket.updated_at = datetime.utcnow()
    _log_activity('ticket_charge_add', f'Added ${amount:.2f} charge to ticket #{ticket.id}: {description}',
                   site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash('Charge added.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/edit', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_edit(charge_id):
    """Corrects a charge's description/amount after the fact — same
    reasoning as admin_incident_fee (the estimate didn't match what the
    office actually decided to charge)."""
    charge = TicketCharge.query.get_or_404(charge_id)
    description = request.form.get('description', '').strip()
    amount = _parse_money(request.form.get('amount'))
    if not description:
        flash('Describe what this charge is for.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))
    if not amount:
        flash('Enter a charge amount.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))
    charge.description = description
    charge.amount = amount
    charge.ticket.updated_at = datetime.utcnow()
    _log_activity('fee_edit', f'Updated charge on ticket #{charge.ticket_id} to ${amount:.2f}: {description}',
                   site_id=charge.ticket.site_id, ticket_id=charge.ticket_id)
    db.session.commit()
    flash('Charge updated.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/delete', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_delete(charge_id):
    charge = TicketCharge.query.get_or_404(charge_id)
    ticket_id, description, amount, site_id = charge.ticket_id, charge.description, charge.amount, charge.ticket.site_id
    db.session.delete(charge)
    _log_activity('ticket_charge_delete', f'Deleted ${amount:.2f} charge from ticket #{ticket_id}: {description}',
                   site_id=site_id, ticket_id=ticket_id)
    db.session.commit()
    flash('Charge deleted.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/mark_paid', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_mark_paid(charge_id):
    charge = TicketCharge.query.get_or_404(charge_id)
    charge.paid_at = datetime.utcnow()
    charge.ticket.updated_at = datetime.utcnow()
    _log_activity('fee_paid', f'Marked ${charge.amount:.2f} charge paid on ticket #{charge.ticket_id}.',
                   site_id=charge.ticket.site_id, ticket_id=charge.ticket_id)
    db.session.commit()
    flash('Marked paid.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))


@app.route('/admin/ticket_categories')
@require_permission('tickets')
def admin_ticket_categories():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = TicketCategory.query
    if search:
        query = query.filter(TicketCategory.name.ilike(f'%{search}%'))
    categories = query.order_by(TicketCategory.name.desc() if sort_dir == 'desc' else TicketCategory.name.asc()).all()
    return render_template('admin_ticket_categories.html', categories=categories, search=search, sort_dir=sort_dir)


@app.route('/admin/ticket_categories/new', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_category_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_ticket_category_form.html', category=None, form=request.form)
        if TicketCategory.query.filter(db.func.lower(TicketCategory.name) == name.lower()).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_ticket_category_form.html', category=None, form=request.form)

        db.session.add(TicketCategory(name=name, default_price=default_price))
        _log_activity('ticket_category_add', f'Added ticket category "{name}".')
        db.session.commit()
        flash(f'Added category "{name}".', 'success')
        return redirect(url_for('admin_ticket_categories'))

    return render_template('admin_ticket_category_form.html', category=None, form=None)


@app.route('/admin/ticket_categories/<int:category_id>/edit', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_category_edit(category_id):
    category = TicketCategory.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        is_active = bool(request.form.get('is_active'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_ticket_category_form.html', category=category, form=None)
        if TicketCategory.query.filter(db.func.lower(TicketCategory.name) == name.lower(),
                                        TicketCategory.id != category_id).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_ticket_category_form.html', category=category, form=None)

        category.name = name
        category.default_price = default_price
        category.is_active = is_active
        _log_activity('ticket_category_edit', f'Edited ticket category "{name}".')
        db.session.commit()
        flash(f'Updated category "{name}".', 'success')
        return redirect(url_for('admin_ticket_categories'))

    return render_template('admin_ticket_category_form.html', category=category, form=None)


@app.route('/admin/ticket_categories/<int:category_id>/delete', methods=['POST'])
@require_permission('tickets')
def admin_ticket_category_delete(category_id):
    category = TicketCategory.query.get_or_404(category_id)
    in_use = Ticket.query.filter_by(category_id=category_id).count()
    if in_use:
        flash(f'Cannot delete "{category.name}" — {in_use} ticket(s) still reference it. Deactivate it instead.', 'error')
        return redirect(url_for('admin_ticket_categories'))
    if TicketAutomation.query.filter_by(ticket_category_id=category_id).first():
        flash(f'Cannot delete "{category.name}" — it has an automation configured. Delete that automation first.', 'error')
        return redirect(url_for('admin_ticket_categories'))
    name = category.name
    db.session.delete(category)
    _log_activity('ticket_category_delete', f'Deleted ticket category "{name}".')
    db.session.commit()
    flash(f'Deleted category "{name}".', 'success')
    return redirect(url_for('admin_ticket_categories'))


@app.route('/admin/automations')
@require_super_admin
def admin_automations():
    """The old per-category ticket automations became general automation
    rules (the migration converted every one) — this keeps old links working."""
    return redirect(url_for('admin_rules'))


@app.route('/admin/pending_actions/<int:action_id>/confirm', methods=['POST'])
@require_permission('devices')
def admin_pending_action_confirm(action_id):
    """Runs a staged PendingDeviceAction now — the one-click confirm for an
    automation that was configured to require it. Reachable from both the
    triggering ticket's page and the device's own page."""
    action = PendingDeviceAction.query.get_or_404(action_id)
    fallback_url = url_for('admin_ticket_detail', ticket_id=action.ticket_id) if action.ticket_id \
        else url_for('admin_asset_assign', asset_tag=action.asset_tag)
    if action.status != 'pending':
        flash('This action was already resolved.', 'info')
        return redirect(request.referrer or fallback_url)

    _, actor_label, _ = _current_actor()
    status, message = _run_pending_device_action(action, resolved_by=actor_label)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/pending_actions/<int:action_id>/dismiss', methods=['POST'])
@require_permission('devices')
def admin_pending_action_dismiss(action_id):
    """Declines a staged PendingDeviceAction without running it — e.g. the
    ticket was mis-categorized, or the device turned out fine."""
    action = PendingDeviceAction.query.get_or_404(action_id)
    fallback_url = url_for('admin_ticket_detail', ticket_id=action.ticket_id) if action.ticket_id \
        else url_for('admin_asset_assign', asset_tag=action.asset_tag)
    if action.status != 'pending':
        flash('This action was already resolved.', 'info')
        return redirect(request.referrer or fallback_url)

    _, actor_label, _ = _current_actor()
    action.status = 'dismissed'
    action.resolved_by_label = actor_label
    action.resolved_at = datetime.utcnow()
    action_label = AUTOMATION_ACTIONS.get(action.action_type, action.action_type)
    _log_activity('automation_dismissed', f'Dismissed pending {action_label} for {action.asset_tag}.',
                   ticket_id=action.ticket_id)
    db.session.commit()
    flash('Dismissed.', 'info')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/assets/<string:asset_tag>/profile_clear', methods=['POST'])
@require_permission('devices')
def admin_asset_profile_clear(asset_tag):
    """Manually sends a Google Workspace profile clear (WIPE_USERS) to this
    device — the same action a Cryptohome Error automation can stage, but
    available directly any time, without waiting for a matching ticket."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    status, message = _execute_device_automation_action('profile_clear', asset_tag)
    if status == 'ok':
        _log_activity('device_profile_clear', f'Sent a profile clear (wipe local users) to {asset_tag}.',
                       site_id=registry_row.site_id)
        db.session.commit()
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
