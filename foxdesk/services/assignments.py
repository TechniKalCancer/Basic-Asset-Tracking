"""Assigning devices to people, loaner checkout/checkin, and overdue lookups."""
from datetime import datetime, timedelta
from foxdesk.core import EMAIL_ENABLED, db, logger
from foxdesk.models import Asset, AssetRegistry, AssignmentHistory, LoanerCheckout, Person
from foxdesk.services.emailer import _render_email_template, send_email
from foxdesk.services.auth import _log_activity
from foxdesk.integrations.google import _sync_device_google_state


def _release_person_assets(person, condition_in):
    """Unassigns every asset currently held by a person. Shared by delete and
    the bulk graduate action. Returns the number of assets released."""
    affected_assets = Asset.query.filter_by(assigned_to_id=person.id).all()
    for asset in affected_assets:
        _close_open_assignment(asset.asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
    return len(affected_assets)


def _close_open_assignment(asset_tag, condition_in=None):
    """Closes the current open AssignmentHistory row for an asset_tag, if any."""
    open_row = AssignmentHistory.query.filter_by(asset_tag=asset_tag, unassigned_at=None).first()
    if open_row:
        open_row.unassigned_at = datetime.utcnow()
        open_row.condition_in = condition_in


def _assign_asset_to_person(asset_tag, person, condition_out=None, due_date=None, acknowledged_by=None):
    """
    Shared assign logic used by both the single assign form and bulk assign.
    The asset_tag must already exist in the registry (caller's responsibility
    to check) — the live Asset row is created here if scanning hasn't made one yet.

    acknowledged_by MUST stay the last parameter — both callers below invoke
    this positionally, so inserting a new param earlier would silently shift
    due_date into the wrong argument with no error.

    Returns:
        (status, message) where status is 'assigned', 'already', or 'error'.
    """
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    if asset and asset.assigned_to_id == person.id:
        return 'already', f'{asset_tag} is already assigned to {person.full_name}.'

    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    if registry_row and registry_row.is_loaner:
        return 'error', (f'{asset_tag} is in the loaner pool and can\'t be permanently assigned — '
                          'check it out as a loaner instead, or remove it from the loaner pool first.')

    try:
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        _close_open_assignment(asset_tag, condition_in='Reassigned')
        db.session.add(AssignmentHistory(
            asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
            condition_out=condition_out, due_date=due_date, acknowledged_by=acknowledged_by,
        ))
        asset.assigned_to_id = person.id
        asset.status = 'assigned'
        _log_activity('device_assign', f'Assigned {asset_tag} to {person.full_name}.',
                       site_id=registry_row.site_id if registry_row else None)
        db.session.commit()
        if registry_row:
            _sync_device_google_state(registry_row, enabled=True, person=person)
        return 'assigned', f'Assigned {asset_tag} to {person.full_name}.'
    except Exception as e:
        db.session.rollback()
        return 'error', f'Could not assign {asset_tag}: {e}'


def _overdue_assignments(site_ids=None):
    """Open assignments (not yet returned) whose due_date has passed.
    site_ids=None means unrestricted (e.g. the background job)."""
    today = datetime.utcnow().date()
    query = AssignmentHistory.query.filter(
        AssignmentHistory.unassigned_at.is_(None),
        AssignmentHistory.due_date.isnot(None),
        AssignmentHistory.due_date < today,
    )
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == AssignmentHistory.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    return query.order_by(AssignmentHistory.due_date).all()


LOANER_REMINDER_RESEND_HOURS = 24


LOANER_DEFAULT_LOAN_DAYS = 7


def _overdue_loaners(site_ids=None):
    """Open loaner checkouts (not yet returned) whose due_date has passed.
    site_ids=None means unrestricted (e.g. the background job)."""
    today = datetime.utcnow().date()
    query = LoanerCheckout.query.filter(
        LoanerCheckout.checked_in_at.is_(None),
        LoanerCheckout.due_date.isnot(None),
        LoanerCheckout.due_date < today,
    )
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    return query.order_by(LoanerCheckout.due_date).all()


def _loaner_reminder_email_content(row, person, now):
    """Builds the subject/body for a loaner reminder email — shared by the
    automatic hourly overdue sweep and the admin's manual Email Selected
    action on /admin/loaners. Unlike the automatic sweep (which only ever
    sees overdue rows), the manual action can target a loaner that isn't
    due yet, so this branches on whether due_date has actually passed to
    pick which customizable template kind (see EMAIL_TEMPLATE_KINDS) applies."""
    variables = {
        'first_name': person.first_name, 'full_name': person.full_name, 'asset_tag': row.asset_tag,
        'due_date': row.due_date.strftime('%Y-%m-%d') if row.due_date else '',
        'days_overdue': '', 'days_overdue_plural': '',
    }
    if row.due_date and row.due_date < now.date():
        days_overdue = (now.date() - row.due_date).days
        variables['days_overdue'] = str(days_overdue)
        variables['days_overdue_plural'] = 's' if days_overdue != 1 else ''
        kind = 'loaner_overdue'
    elif row.due_date:
        kind = 'loaner_upcoming'
    else:
        kind = 'loaner_nodate'
    return _render_email_template(kind, variables)


def _send_overdue_loaner_reminders(site_ids=None):
    """
    Emails anyone with an overdue loaner. Safe to call repeatedly (e.g. from a
    background loop) — reminder_sent_at gates re-sending to once per
    LOANER_REMINDER_RESEND_HOURS, so it won't spam the same student hourly.
    site_ids=None (the background loop's case) means every site.
    Returns (sent, failed, skipped) counts.
    """
    if not EMAIL_ENABLED:
        return 0, 0, 0

    sent = failed = skipped = 0
    now = datetime.utcnow()
    for row in _overdue_loaners(site_ids):
        if row.reminder_sent_at and (now - row.reminder_sent_at).total_seconds() < LOANER_REMINDER_RESEND_HOURS * 3600:
            continue
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        subject, body = _loaner_reminder_email_content(row, person, now)
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = now
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Loaner reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    if sent:
        _log_activity('reminders_send', f'Sent {sent} overdue-loaner reminder(s).')
    db.session.commit()
    return sent, failed, skipped


def _checkout_loaner(asset_tag, person, due_date=None, site_ids=None, acknowledged_by=None, repair_id=None):
    """Shared checkout logic used by both the admin page and student self-service.

    repair_id links this checkout to an open Repair (see admin_repair_assign_loaner)
    — someone's own device is out for repair and this loaner covers them in the
    meantime. A repair-linked checkout deliberately gets NO default due date
    (a repair's turnaround isn't a fixed N-day loan like a normal checkout), so
    it never shows up as "overdue" just because repair is taking a while.

    repair_id MUST stay the last parameter — callers invoke this positionally,
    so inserting a new param earlier would silently shift site_ids into the
    wrong argument with no error."""
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag, is_loaner=True).first()
    if not row:
        return 'error', f'{asset_tag} is not a loaner device.'
    if site_ids is not None and row.site_id not in site_ids:
        return 'error', f'{asset_tag} is not one of your site\'s loaners.'
    already_out = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if already_out:
        return 'error', f'{asset_tag} is already checked out to {already_out.person_name}.'
    resolved_due_date = due_date
    if resolved_due_date is None and not repair_id:
        resolved_due_date = datetime.utcnow().date() + timedelta(days=LOANER_DEFAULT_LOAN_DAYS)
    db.session.add(LoanerCheckout(
        asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
        due_date=resolved_due_date, acknowledged_by=acknowledged_by, repair_id=repair_id,
    ))
    log_message = f'Checked out loaner {asset_tag} to {person.full_name}.' if not repair_id \
        else f'Checked out repair loaner {asset_tag} to {person.full_name}.'
    _log_activity('loaner_checkout', log_message, site_id=row.site_id)
    db.session.commit()
    _sync_device_google_state(row, enabled=True, person=person)
    message = f'Checked out {asset_tag} to {person.full_name}.'
    if row.site_id and person.site_id and row.site_id != person.site_id:
        message += ' Note: this loaner and person are at different sites.'
    return 'ok', message


def _checkin_loaner(asset_tag, condition_notes=None, site_ids=None):
    """Shared checkin logic used by both the admin page and student self-service."""
    open_row = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if not open_row:
        return 'error', f'{asset_tag} is not currently checked out as a loaner.'
    if site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            return 'error', f'{asset_tag} is not one of your site\'s loaners.'
    open_row.checked_in_at = datetime.utcnow()
    if condition_notes:
        open_row.condition_notes = condition_notes
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    _log_activity('loaner_checkin', f'Checked in loaner {asset_tag} (was with {open_row.person_name}).',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    if registry_row:
        _sync_device_google_state(registry_row, enabled=False)
    return 'ok', f'Checked in {asset_tag} (was with {open_row.person_name}).'
