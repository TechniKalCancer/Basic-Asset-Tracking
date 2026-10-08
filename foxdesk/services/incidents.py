"""Damage/loss incidents, fees, and guardian damage notices."""
from datetime import datetime
from decimal import Decimal
from foxdesk.core import EMAIL_ENABLED, db, logger
from foxdesk.models import AssetRegistry, Incident, Person, Ticket, TicketCharge
from foxdesk.services.emailer import _render_email_template, send_email
from foxdesk.services.auth import _log_activity


def _person_unpaid_fee_total(person_id):
    """Sum of fee_amount across this person's charged-but-unpaid incidents AND
    ticket charges. Used to warn (not block) on delete/graduate."""
    incident_total = db.session.query(db.func.coalesce(db.func.sum(Incident.fee_amount), 0)).filter(
        Incident.person_id == person_id, Incident.fee_charged.is_(True), Incident.paid_at.is_(None),
    ).scalar()
    charge_total = db.session.query(db.func.coalesce(db.func.sum(TicketCharge.amount), 0)) \
        .join(Ticket, Ticket.id == TicketCharge.ticket_id) \
        .filter(Ticket.requester_person_id == person_id, TicketCharge.paid_at.is_(None)).scalar()
    return Decimal(incident_total) + Decimal(charge_total)


def _create_incident(asset_tag, person, description, fee_charged=False, fee_amount=None, repair_category_id=None):
    """Shared incident-logging logic used by both the admin page and the
    student self-service Report a Problem page. Does not commit — caller's
    responsibility, matching _assign_asset_to_person/_checkout_loaner."""
    incident = Incident(
        asset_tag=asset_tag, person_id=person.id if person else None,
        person_name=person.full_name if person else None,
        description=description, fee_charged=fee_charged, fee_amount=fee_amount,
        repair_category_id=repair_category_id,
    )
    db.session.add(incident)
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    _log_activity('incident_add', f'Logged incident on {asset_tag}: {description}',
                   site_id=registry_row.site_id if registry_row else None)
    return incident


def _send_damage_notice(incident):
    """Emails the damage_notice template to the guardian of the student the
    incident was logged against. Sent synchronously (unlike the ticket
    notices) because it's always an explicit office action and the person
    clicking needs to know whether it actually went out. Commits the
    guardian_notified_at stamp. Returns (ok, message)."""
    person = Person.query.get(incident.person_id) if incident.person_id else None
    if not EMAIL_ENABLED:
        return False, 'Email isn\'t configured (set SMTP_FROM_EMAIL), so no notice was sent.'
    if not person or not person.guardian_email:
        return False, 'No parent/guardian email on file for this person — add one on their People record.'
    if incident.fee_charged and incident.fee_amount:
        fee_line = f'A fee of ${incident.fee_amount:.2f} has been assessed.'
    elif incident.fee_charged:
        fee_line = 'A fee will be assessed; the office will follow up with the amount.'
    else:
        fee_line = 'No fee has been assessed at this time.'
    subject, body = _render_email_template('damage_notice', {
        'guardian_name': person.guardian_name or 'Parent/Guardian',
        'student_name': person.full_name, 'asset_tag': incident.asset_tag,
        'incident_date': incident.created_at.strftime('%Y-%m-%d'),
        'incident_description': incident.description, 'fee_line': fee_line,
    })
    try:
        send_email(person.guardian_email, subject, body)
    except Exception as e:
        logger.error('Damage notice for incident %s failed: %s', incident.id, e)
        return False, f'Could not send the notice: {e}'
    incident.guardian_notified_at = datetime.utcnow()
    registry_row = AssetRegistry.query.filter_by(asset_tag=incident.asset_tag).first()
    _log_activity('damage_notice', f'Emailed damage notice for {incident.asset_tag} to {person.full_name}\'s guardian ({person.guardian_email}).',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    return True, f'Damage notice emailed to {person.guardian_email}.'
