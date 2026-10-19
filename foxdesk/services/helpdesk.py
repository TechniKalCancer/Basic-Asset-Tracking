"""Tickets, repairs (which open tickets), requester emails, and ticket automations."""
from foxdesk.clock import utcnow
from foxdesk.core import EMAIL_ENABLED, GOOGLE_SYNC_ENABLED, db
from foxdesk.models import (
    AUTOMATION_ACTIONS,
    Asset,
    AssetRegistry,
    REPAIR_OUTCOMES,
    Repair,
    RepairCategory,
    RepairNote,
    TICKET_PRIORITIES,
    Ticket,
    TicketComment,
    TicketAutomation,
    TicketCategory,
    TicketCharge,
)
from foxdesk.services.emailer import _get_email_settings, _render_email_template, _send_email_in_background
from foxdesk.services.auth import _log_activity
from foxdesk.services.scoping import _scope_registry
from foxdesk.integrations.google import (
    move_chromeos_device_to_ou,
    set_chromeos_device_enabled,
    wipe_chromeos_device_users,
)


def _get_or_create_repair_ticket_category():
    """Ensures a 'Device Repair' ticket category exists to file auto-opened
    repair tickets under, so a repair shows up in the normal help-desk queue
    instead of living invisibly outside it."""
    category = TicketCategory.query.filter_by(name='Device Repair').first()
    if not category:
        category = TicketCategory(name='Device Repair', is_active=True)
        db.session.add(category)
        db.session.flush()  # assigns category.id for the ticket we're about to create
    return category


def _open_ticket_for_device(asset_tag):
    """The device's newest open ticket that has no repair yet — a repair
    started from it belongs on it, rather than on a second ticket."""
    return (Ticket.query.outerjoin(Repair, Repair.ticket_id == Ticket.id)
            .filter(Ticket.asset_tag == asset_tag, Ticket.status.in_(('open', 'in_progress')),
                    Ticket.merged_into_id.is_(None), Repair.id.is_(None))
            .order_by(Ticket.created_at.desc()).first())


def _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description, expected_return_at, site_ids=None,
                           ticket=None):
    """Shared repair-creation logic used by both the device-page form and the
    quick Send to Repair flow on /admin/repairs — creates the tracking
    record, opens a matching help-desk Ticket (so repairs show up in the
    normal ticket queue/history, not just the Repairs page), and sets the
    asset status to 'repair', all together. Does not commit; returns
    (status, message) same as the _checkout_loaner/_checkin_loaner pattern.
    issue_description is required (checked here so both callers get the same
    validation for free); repair_category_id is optional, same reasoning as
    Incident's repair_category_id.

    The repair goes on `ticket` when given (sent from a ticket), else on the
    device's open ticket if it has one, and only otherwise on a new ticket."""
    issue_description = (issue_description or '').strip()
    if not issue_description:
        return 'error', 'Describe the issue before sending a device to repair.'
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first()
    if not registry_row:
        return 'error', f'"{asset_tag}" was not found in the asset registry.'
    if Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first():
        return 'error', f'{asset_tag} already has an open repair.'
    from foxdesk.services.overdue import repair_limit_date, repair_limit_problem
    now = utcnow()
    problem = repair_limit_problem(now, expected_return_at)
    if problem:
        return 'error', problem
    expected_return_at = expected_return_at or repair_limit_date(now)

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None

    repair_category = db.session.get(RepairCategory, repair_category_id) if repair_category_id else None
    if ticket is not None and ticket.repair is not None:
        return 'error', f'Ticket #{ticket.id} already has a repair.'
    ticket = ticket or _open_ticket_for_device(asset_tag)
    reused = ticket is not None
    if not reused:
        ticket_category = _get_or_create_repair_ticket_category()
        subject = f'Repair: {asset_tag}' + (f' ({repair_category.name})' if repair_category else '')
        ticket = _create_ticket(
            ticket_category.id, subject, issue_description,
            person=person, asset_tag=asset_tag, site_id=registry_row.site_id,
        )
    else:
        db.session.add(TicketComment(ticket_id=ticket.id, author_label='FoxDesk',
                                     body=f'Sent {asset_tag} to repair: {issue_description}'))
        ticket.asset_tag = ticket.asset_tag or asset_tag
        ticket.updated_at = now

    db.session.add(Repair(
        asset_tag=asset_tag, repair_category_id=repair_category.id if repair_category else None,
        ticket_number=ticket_number, issue_description=issue_description, expected_return_at=expected_return_at,
        person_name_snapshot=person.full_name if person else None, ticket_id=ticket.id, sent_at=now,
        status='at_vendor' if ticket_number else 'received', status_changed_at=now,
    ))
    if not asset:
        asset = Asset(asset_tag=asset_tag, is_valid=True)
        db.session.add(asset)
    asset.status = 'repair'
    _log_activity('repair_send', f'Sent {asset_tag} to repair{" (" + repair_category.name + ")" if repair_category else ""}.',
                   site_id=registry_row.site_id, ticket_id=ticket.id)
    if reused:
        return 'ok', f'{asset_tag} sent to repair — on its ticket #{ticket.id}.'
    return 'ok', f'{asset_tag} sent to repair — opened ticket #{ticket.id}.'


def close_repair(repair, outcome, notes=None, actor_label=None, close_ticket=True):
    """Closes an open repair the one way both the repair and ticket pages
    use: records the outcome, puts the device back in service (Fixed) or
    retires it (Could Not Repair / Replaced), notes it on the timeline and
    the ticket, and with close_ticket resolves the ticket too. Does not
    commit. Returns (the ticket's status before, or None if it wasn't
    resolved here; the repair's loaner still out, or None)."""
    from foxdesk.models import LoanerCheckout
    from foxdesk.services.sla import mark_responded
    now = utcnow()
    repair.returned_at, repair.outcome, repair.notes = now, outcome, notes
    label = REPAIR_OUTCOMES[outcome]
    asset = Asset.query.filter_by(asset_tag=repair.asset_tag).first()
    if asset:
        if outcome == 'fixed':
            asset.status = 'assigned' if asset.assigned_to_id else 'available'
        else:
            asset.status = 'retired'
    db.session.add(RepairNote(repair_id=repair.id, author_label=actor_label, created_at=now,
                              body=f'Returned: {label}.' + (f' {notes}' if notes else '')))
    resolved_from = None
    ticket = repair.ticket
    if ticket is not None:
        db.session.add(TicketComment(ticket_id=ticket.id, author_label=actor_label or 'FoxDesk',
                                     body=f'Returned from repair: {label}.' + (f' {notes}' if notes else '')))
        ticket.updated_at = now
        if close_ticket and ticket.status in ('open', 'in_progress'):
            resolved_from = ticket.status
            ticket.status, ticket.resolved_at = 'resolved', ticket.resolved_at or now
            mark_responded(ticket, now)
            _log_activity('ticket_status', f'Ticket #{ticket.id} resolved: its repair is back ({label}).',
                          site_id=ticket.site_id, ticket_id=ticket.id)
    registry_row = AssetRegistry.query.filter_by(asset_tag=repair.asset_tag).first()
    _log_activity('repair_return', f'{repair.asset_tag} returned from repair ({label}).',
                  site_id=registry_row.site_id if registry_row else None, ticket_id=repair.ticket_id)
    loaner = LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first()
    return resolved_from, loaner


def link_repair_to_ticket(repair, ticket, actor_label=None):
    """Puts a repair on a ticket (each has at most one of the other).
    Raises ValueError when it can't. Does not commit. Returns the ticket
    the repair was on before, if any."""
    if ticket.merged_into_id:
        raise ValueError(f'Ticket #{ticket.id} was merged into #{ticket.merged_into_id}; use that one.')
    if ticket.repair is not None and ticket.repair.id != repair.id:
        raise ValueError(f'Ticket #{ticket.id} already has a repair ({ticket.repair.asset_tag}).')
    previous = repair.ticket if repair.ticket_id and repair.ticket_id != ticket.id else None
    if repair.ticket_id == ticket.id:
        return None
    repair.ticket_id = ticket.id
    ticket.asset_tag = ticket.asset_tag or repair.asset_tag
    ticket.updated_at = utcnow()
    db.session.add(TicketComment(ticket_id=ticket.id, author_label=actor_label or 'FoxDesk',
                                 body=f'Linked the repair of {repair.asset_tag} to this ticket.'
                                      + (f' It was on ticket #{previous.id}.' if previous else '')))
    if previous is not None:
        db.session.add(TicketComment(ticket_id=previous.id, author_label=actor_label or 'FoxDesk',
                                     body=f'The repair of {repair.asset_tag} moved to ticket #{ticket.id}.'))
        previous.updated_at = utcnow()
    _log_activity('repair_edit', f'Linked the repair of {repair.asset_tag} to ticket #{ticket.id}.',
                  site_id=ticket.site_id, ticket_id=ticket.id)
    return previous


def _create_ticket(category_id, subject, description, person=None, requester_name=None,
                    requester_email=None, asset_tag=None, site_id=None, priority='normal', waive_fee_by=None):
    """Shared ticket-creation logic used by both the public submission page and
    the admin-initiated New Ticket form. Does not commit — caller's responsibility.
    A category with a default_price seeds one initial itemized TicketCharge at
    creation time (correctable/removable later, more can be added as the
    ticket progresses) — no charge fields shown on either creation form,
    matching report_problem_page's "not the submitter's decision" reasoning.
    waive_fee_by (a tech's name) adds that charge already waived, so the
    ticket still shows what the fee would have been."""
    category = db.session.get(TicketCategory, category_id)
    default_price = category.default_price if category else None
    ticket = Ticket(
        category_id=category_id, subject=subject, description=description,
        priority=priority if priority in TICKET_PRIORITIES else 'normal',
        site_id=site_id, asset_tag=asset_tag or None,
        requester_person_id=person.id if person else None,
        requester_name=person.full_name if person else (requester_name or None),
        requester_email=person.email if person else (requester_email or None),
    )
    db.session.add(ticket)
    db.session.flush()  # assigns ticket.id, needed so the log entry (and any charge) can link back to it
    from foxdesk.services.sla import apply_sla
    apply_sla(ticket)
    if default_price:
        charge = TicketCharge(ticket_id=ticket.id, description=category.name, amount=default_price)
        if waive_fee_by:
            charge.waived_at, charge.waived_by = utcnow(), waive_fee_by[:160]
            charge.waive_reason = 'Waived when the ticket was opened'
        db.session.add(charge)
    _log_activity('ticket_add', f'Opened ticket #{ticket.id}: {subject}', site_id=site_id, ticket_id=ticket.id)
    return ticket


def _ticket_email_vars(ticket):
    first_name = (ticket.requester.first_name if ticket.requester
                  else (ticket.requester_name or 'there').split(' ')[0])
    return {
        'first_name': first_name, 'full_name': ticket.requester_name or '',
        'ticket_id': str(ticket.id), 'ticket_subject': ticket.subject,
        'ticket_description': ticket.description, 'ticket_status': ticket.status.replace('_', ' '),
    }


def _notify_ticket_requester(ticket, kind, extra_vars=None, force=False):
    """Emails the ticket's requester using the `kind` template. Automatic
    notices (received/resolved) respect the ticket_notifications_enabled
    switch on /admin/emails; an explicit tech reply passes force=True.
    Returns True if a send was queued — False (silently) when email isn't
    configured, there's no requester address, or notices are switched off,
    since none of those should block the ticket action itself."""
    if not EMAIL_ENABLED or not ticket.requester_email:
        return False
    if not force and not _get_email_settings().ticket_notifications_enabled:
        return False
    variables = _ticket_email_vars(ticket)
    variables.update(extra_vars or {})
    subject, body = _render_email_template(kind, variables)
    from foxdesk.services.inbound_mail import reply_headers
    reply_to, headers = reply_headers(ticket.id)
    if reply_to and f'#{ticket.id}' not in subject:
        subject = f'{subject} [Ticket #{ticket.id}]'  # an edited template might drop it; replies need it to thread
    if reply_to:
        _send_email_in_background(ticket.requester_email, subject, body, reply_to=reply_to, headers=headers)
    else:
        _send_email_in_background(ticket.requester_email, subject, body)
    return True


def _execute_device_automation_action(action_type, asset_tag, ticket=None, automation=None, params=None):
    """
    Runs one automation action_type against a device by asset_tag —
    structured as a dispatch so one more action type is just one more
    branch here. Never raises — mirrors _checkin_loaner/_checkout_loaner's
    (status, message) contract so callers don't each need their own
    try/except around it.

    ticket and automation are optional extra context some actions need:
    'send_to_repair' pulls the issue description from the triggering
    ticket (falling back to a generic one for the manual/no-ticket case),
    and its repair category — like 'move_device's target org unit — comes
    from the automation's own config, since neither has anywhere else to
    come from when this fires automatically with no human filling out a
    form.

    params is the same settings from an automation rule instead
    ({'repair_category': id, 'org_unit': path}); it wins over automation.
    """
    params = params or {}
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    if not registry_row:
        return 'error', f'"{asset_tag}" was not found in the asset registry.'

    if action_type == 'profile_clear':
        if not GOOGLE_SYNC_ENABLED:
            return 'error', 'Google Workspace sync isn\'t configured yet.'
        if not registry_row.serial_number:
            return 'error', f'{asset_tag} has no serial number on file to look up.'
        try:
            wipe_chromeos_device_users(registry_row.serial_number)
            return 'ok', f'Profile clear sent to {asset_tag} — runs next time the device checks in.'
        except LookupError as e:
            return 'error', str(e)
        except Exception as e:
            return 'error', f'Could not send profile clear: {e}'

    if action_type == 'disable_google':
        if not GOOGLE_SYNC_ENABLED:
            return 'error', 'Google Workspace sync isn\'t configured yet.'
        if not registry_row.serial_number:
            return 'error', f'{asset_tag} has no serial number on file to look up.'
        try:
            set_chromeos_device_enabled(registry_row.serial_number, False)
        except LookupError as e:
            return 'error', str(e)
        except Exception as e:
            return 'error', f'Could not disable device: {e}'
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.google_enabled = False
        asset.google_last_sync_at = utcnow()
        return 'ok', f'{asset_tag} disabled in Google Workspace.'

    if action_type == 'send_to_repair':
        repair_category_id = (int(params['repair_category']) if str(params.get('repair_category') or '').isdigit()
                              else (automation.repair_category_id if automation else None))
        if ticket:
            issue_description = f'Auto-sent to repair via automation from ticket #{ticket.id}: {ticket.subject}'
        else:
            issue_description = f'Automated Send to Repair for {asset_tag}.'
        return _send_device_to_repair(asset_tag, repair_category_id, None, issue_description, None, site_ids=None,
                                      ticket=ticket if ticket is not None and ticket.repair is None else None)

    if action_type == 'move_device':
        target_ou = params.get('org_unit') or (automation.target_org_unit_path if automation else None)
        if not target_ou:
            return 'error', 'No target org unit is configured on this automation.'
        if not GOOGLE_SYNC_ENABLED:
            return 'error', 'Google Workspace sync isn\'t configured yet.'
        if not registry_row.serial_number:
            return 'error', f'{asset_tag} has no serial number on file to look up.'
        try:
            move_chromeos_device_to_ou(registry_row.serial_number, target_ou)
            return 'ok', f'{asset_tag} moved to {target_ou}.'
        except LookupError as e:
            return 'error', str(e)
        except Exception as e:
            return 'error', f'Could not move device: {e}'

    return 'error', f'Unknown automation action "{action_type}".'


def _run_pending_device_action(action, resolved_by='Automatic', automation=None):
    """Actually executes a staged PendingDeviceAction — called either
    immediately (its TicketAutomation skips confirmation) or from the
    admin confirm route. Updates the row's status/resolved fields and logs
    the outcome either way, then commits. resolved_by is the confirming
    admin's actor_label, or 'Automatic' when no human was involved.
    automation is passed straight through when the immediate-fire caller
    already has it in hand; the confirm route doesn't, so it's re-derived
    here from the ticket's category — cheap, and avoids needing a second
    FK just to remember which automation staged a given action."""
    if automation is None and action.ticket and not action.rule_id:
        automation = TicketAutomation.query.filter_by(ticket_category_id=action.ticket.category_id).first()
    status, message = _execute_device_automation_action(
        action.action_type, action.asset_tag, ticket=action.ticket, automation=automation, params=action.params)
    action.status = 'confirmed' if status == 'ok' else 'failed'
    action.error_message = None if status == 'ok' else message
    action.resolved_at = utcnow()
    action.resolved_by_label = resolved_by
    action_label = AUTOMATION_ACTIONS.get(action.action_type, action.action_type)
    outcome = 'done' if status == 'ok' else f'failed — {message}'
    _log_activity('automation_run', f'{action_label} on {action.asset_tag}: {outcome}', ticket_id=action.ticket_id)
    db.session.commit()
    return status, message
