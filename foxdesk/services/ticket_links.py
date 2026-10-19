"""Merging duplicate tickets and linking related ones.

Merge — the same request twice (someone emailed, then walked up and filed
it again): everything on the duplicate moves to the ticket that stays, the
duplicate is closed and points at it, and email replies to the duplicate's
messages land on the one that stays.

Link — one problem, many reports ("Wi-Fi down in B-wing" from twelve
teachers): each report stays its own ticket under the main one, and
resolving the main ticket can resolve them all and tell every requester.
Links are one level deep; linking under a ticket that is itself linked
uses its main ticket instead.
"""
from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import Attachment, PartMovement, Ticket, TicketCharge, TicketComment
from foxdesk.services.auth import _log_activity


class LinkError(ValueError):
    """Shown to the tech as-is."""


def final_ticket(ticket):
    """Follow merges to the ticket that's still in use."""
    seen = set()
    while ticket is not None and ticket.merged_into_id and ticket.id not in seen:
        seen.add(ticket.id)
        ticket = db.session.get(Ticket, ticket.merged_into_id)
    return ticket


def merge_tickets(duplicate, keep, actor_label):
    """Fold `duplicate` into `keep`. Does not commit; returns a summary."""
    if duplicate.id == keep.id:
        raise LinkError('A ticket can\'t be merged into itself.')
    if duplicate.merged_into_id:
        raise LinkError(f'Ticket #{duplicate.id} was already merged into #{duplicate.merged_into_id}.')
    if keep.merged_into_id:
        raise LinkError(f'Ticket #{keep.id} was merged into #{keep.merged_into_id} — merge into that one instead.')
    if duplicate.repair is not None:
        raise LinkError(f'Ticket #{duplicate.id} tracks a repair, so merge the other ticket into it instead.')
    moved = []
    comments = TicketComment.query.filter_by(ticket_id=duplicate.id).all()
    for c in comments:
        c.ticket = keep  # through the relationship, so neither ticket's loaded list goes stale
    if comments:
        moved.append(f'{len(comments)} comment(s)')
    charges = TicketCharge.query.filter_by(ticket_id=duplicate.id).all()
    for c in charges:
        c.ticket = keep
    if charges:
        moved.append(f'{len(charges)} charge(s)')
    files = Attachment.query.filter_by(owner_type='ticket', owner_id=duplicate.id).all()
    for a in files:
        a.owner_id = keep.id
    if files:
        moved.append(f'{len(files)} file(s)')
    parts = PartMovement.query.filter_by(ticket_id=duplicate.id).all()
    for p in parts:
        p.ticket_id = keep.id
    linked = Ticket.query.filter_by(parent_id=duplicate.id).all()
    for t in linked:
        t.parent_id = keep.id if t.id != keep.id else None
    if not keep.asset_tag and duplicate.asset_tag:
        keep.asset_tag = duplicate.asset_tag

    who = duplicate.requester_name or duplicate.requester_email or 'unknown requester'
    db.session.add(TicketComment(
        ticket_id=keep.id, author_label=actor_label,
        body=f'Merged ticket #{duplicate.id} into this one ({who}, opened {duplicate.created_at:%Y-%m-%d}).\n\n'
             f'Its original request:\n{duplicate.subject}\n{duplicate.description}'))
    now = utcnow()
    duplicate.merged_into_id = keep.id
    duplicate.parent_id = None
    duplicate.status, duplicate.resolved_at, duplicate.updated_at = 'closed', duplicate.resolved_at or now, now
    keep.updated_at = now
    summary = f'Merged ticket #{duplicate.id} into #{keep.id}' + (f' (moved {", ".join(moved)})' if moved else '') + '.'
    _log_activity('ticket_merge', summary, site_id=keep.site_id, ticket_id=keep.id)
    _log_activity('ticket_merge', summary, site_id=duplicate.site_id, ticket_id=duplicate.id)
    return summary


def link_ticket(ticket, main):
    """Put `ticket` under `main` (or under main's own main ticket). Does
    not commit; returns the main ticket used."""
    if Ticket.query.filter_by(parent_id=ticket.id).first():
        raise LinkError(f'Ticket #{ticket.id} already has tickets linked under it. Link #{main.id} under it instead.')
    main = main.parent if main.parent_id else main
    if ticket.id == main.id:
        raise LinkError('A ticket can\'t be linked to itself.')
    if main.merged_into_id or ticket.merged_into_id:
        raise LinkError('That ticket was merged into another one. Use the ticket it was merged into.')
    ticket.parent_id = main.id
    ticket.updated_at = utcnow()
    _log_activity('ticket_link', f'Linked ticket #{ticket.id} under #{main.id}: {main.subject}',
                  site_id=ticket.site_id, ticket_id=ticket.id)
    _log_activity('ticket_link', f'Linked ticket #{ticket.id} under this one: {ticket.subject}',
                  site_id=main.site_id, ticket_id=main.id)
    return main


def unlink_ticket(ticket):
    main_id = ticket.parent_id
    ticket.parent_id = None
    ticket.updated_at = utcnow()
    _log_activity('ticket_link', f'Unlinked ticket #{ticket.id} from #{main_id}.', site_id=ticket.site_id, ticket_id=ticket.id)


def repair_still_out(ticket):
    return ticket.repair is not None and ticket.repair.returned_at is None


def resolve_linked(main, message, email_requesters, actor_label):
    """Resolve every open ticket linked under `main` with the same note;
    returns ([(ticket, its old status)], the tickets whose requester should
    be emailed). A ticket whose repair is still out stays open: closing it
    closes the repair, which needs an outcome. Does not commit."""
    from foxdesk.services.sla import mark_responded
    now = utcnow()
    resolved, to_email = [], []
    for t in Ticket.query.filter(Ticket.parent_id == main.id, Ticket.status.in_(['open', 'in_progress'])):
        if repair_still_out(t):
            continue
        emailed = bool(email_requesters and message and t.requester_email)
        if message:
            db.session.add(TicketComment(ticket_id=t.id, body=message, author_label=actor_label,
                                         emailed_to_requester=emailed))
        resolved.append((t, t.status))
        t.status, t.resolved_at, t.updated_at = 'resolved', now, now
        mark_responded(t, now)
        _log_activity('ticket_status', f'Ticket #{t.id} resolved along with #{main.id}.', site_id=t.site_id, ticket_id=t.id)
        if emailed:
            to_email.append(t)
    return resolved, to_email
