"""Tickets, repairs (which open tickets), requester emails, and ticket automations."""
from datetime import datetime
from foxdesk.core import EMAIL_ENABLED, GOOGLE_SYNC_ENABLED, db
from foxdesk.models import (
    AUTOMATION_ACTIONS,
    Asset,
    AssetRegistry,
    PendingDeviceAction,
    Repair,
    RepairCategory,
    TICKET_PRIORITIES,
    Ticket,
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


def _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description, expected_return_at, site_ids=None):
    """Shared repair-creation logic used by both the device-page form and the
    quick Send to Repair flow on /admin/repairs — creates the tracking
    record, opens a matching help-desk Ticket (so repairs show up in the
    normal ticket queue/history, not just the Repairs page), and sets the
    asset status to 'repair', all together. Does not commit; returns
    (status, message) same as the _checkout_loaner/_checkin_loaner pattern.
    issue_description is required (checked here so both callers get the same
    validation for free); repair_category_id is optional, same reasoning as
    Incident's repair_category_id."""
    issue_description = (issue_description or '').strip()
    if not issue_description:
        return 'error', 'Describe the issue before sending a device to repair.'
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first()
    if not registry_row:
        return 'error', f'"{asset_tag}" was not found in the asset registry.'
    if Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first():
        return 'error', f'{asset_tag} already has an open repair.'

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None

    repair_category = RepairCategory.query.get(repair_category_id) if repair_category_id else None
    ticket_category = _get_or_create_repair_ticket_category()
    subject = f'Repair: {asset_tag}' + (f' ({repair_category.name})' if repair_category else '')
    ticket = _create_ticket(
        ticket_category.id, subject, issue_description,
        person=person, asset_tag=asset_tag, site_id=registry_row.site_id,
    )

    db.session.add(Repair(
        asset_tag=asset_tag, repair_category_id=repair_category.id if repair_category else None,
        ticket_number=ticket_number, issue_description=issue_description, expected_return_at=expected_return_at,
        person_name_snapshot=person.full_name if person else None, ticket_id=ticket.id,
    ))
    if not asset:
        asset = Asset(asset_tag=asset_tag, is_valid=True)
        db.session.add(asset)
    asset.status = 'repair'
    _log_activity('repair_send', f'Sent {asset_tag} to repair{" (" + repair_category.name + ")" if repair_category else ""}.',
                   site_id=registry_row.site_id)
    return 'ok', f'{asset_tag} sent to repair — opened ticket #{ticket.id}.'


def _create_ticket(category_id, subject, description, person=None, requester_name=None,
                    requester_email=None, asset_tag=None, site_id=None, priority='normal'):
    """Shared ticket-creation logic used by both the public submission page and
    the admin-initiated New Ticket form. Does not commit — caller's responsibility.
    A category with a default_price seeds one initial itemized TicketCharge at
    creation time (correctable/removable later, more can be added as the
    ticket progresses) — no charge fields shown on either creation form,
    matching report_problem_page's "not the submitter's decision" reasoning."""
    category = TicketCategory.query.get(category_id)
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
    if default_price:
        db.session.add(TicketCharge(ticket_id=ticket.id, description=category.name, amount=default_price))
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
    _send_email_in_background(ticket.requester_email, subject, body)
    return True


def _execute_device_automation_action(action_type, asset_tag, ticket=None, automation=None):
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
    """
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
        asset.google_last_sync_at = datetime.utcnow()
        return 'ok', f'{asset_tag} disabled in Google Workspace.'

    if action_type == 'send_to_repair':
        repair_category_id = automation.repair_category_id if automation else None
        if ticket:
            issue_description = f'Auto-sent to repair via automation from ticket #{ticket.id}: {ticket.subject}'
        else:
            issue_description = f'Automated Send to Repair for {asset_tag}.'
        return _send_device_to_repair(asset_tag, repair_category_id, None, issue_description, None, site_ids=None)

    if action_type == 'move_device':
        target_ou = automation.target_org_unit_path if automation else None
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
    if automation is None and action.ticket:
        automation = TicketAutomation.query.filter_by(ticket_category_id=action.ticket.category_id).first()
    status, message = _execute_device_automation_action(
        action.action_type, action.asset_tag, ticket=action.ticket, automation=automation)
    action.status = 'confirmed' if status == 'ok' else 'failed'
    action.error_message = None if status == 'ok' else message
    action.resolved_at = datetime.utcnow()
    action.resolved_by_label = resolved_by
    action_label = AUTOMATION_ACTIONS.get(action.action_type, action.action_type)
    outcome = 'done' if status == 'ok' else f'failed — {message}'
    _log_activity('automation_run', f'{action_label} on {action.asset_tag}: {outcome}', ticket_id=action.ticket_id)
    db.session.commit()
    return status, message


def _check_ticket_automation(ticket):
    """
    Checks whether the ticket's category has an active TicketAutomation
    configured, and if so, either stages it as a PendingDeviceAction for
    an admin to confirm, or fires it immediately — per that automation's
    own require_confirmation setting. No-ops if there's no device attached
    to the ticket (nothing to act on) or no matching automation.

    Called by each ticket-creation route AFTER its own db.session.commit()
    has already succeeded, same reasoning as _sync_device_google_state
    being called post-commit elsewhere in this file: a Google-side failure
    here shouldn't affect whether the ticket itself was saved.
    """
    if not ticket.asset_tag:
        return
    automation = TicketAutomation.query.filter_by(
        ticket_category_id=ticket.category_id, is_active=True).first()
    if not automation:
        return

    action = PendingDeviceAction(
        ticket_id=ticket.id, asset_tag=ticket.asset_tag,
        action_type=automation.action_type, status='pending',
    )
    db.session.add(action)
    db.session.flush()

    if automation.require_confirmation:
        action_label = AUTOMATION_ACTIONS.get(automation.action_type, automation.action_type)
        _log_activity('automation_staged', f'Staged {action_label} for {ticket.asset_tag} — awaiting confirmation.',
                       ticket_id=ticket.id)
        db.session.commit()
    else:
        _run_pending_device_action(action, resolved_by='Automatic', automation=automation)
