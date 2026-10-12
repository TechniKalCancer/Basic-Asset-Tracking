"""Pages: help desk tools — saved replies, signatures, teams, and who's on a ticket."""
from datetime import date, timedelta

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.models import CannedReply, Site, Team, Ticket, TicketCategory, TicketPresence, User
from foxdesk.services.auth import _current_actor, _current_site_ids, _current_user, _log_activity, require_permission
from foxdesk.services.scoping import _scope_tickets, _ticket_assignees

PRESENCE_SECONDS = 60


class _Blank(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def render_canned(body, ticket, tech_name):
    """Fill a saved reply's {placeholders} from the ticket."""
    first = (ticket.requester.first_name if ticket.requester
             else (ticket.requester_name or 'there').split(' ')[0])
    values = _Blank(first_name=first, full_name=ticket.requester_name or '', ticket_id=ticket.id,
                    ticket_subject=ticket.subject, asset_tag=ticket.asset_tag or '', tech_name=tech_name,
                    today=date.today().strftime('%B %d, %Y'))
    try:
        return body.format_map(values)
    except (ValueError, IndexError):
        return body


# ─── saved replies ────────────────────────────────────────────────────────────

def _visible_replies():
    me = _current_user()
    return CannedReply.query.filter(db.or_(CannedReply.user_id.is_(None), CannedReply.user_id == (me.id if me else -1)))


@app.route('/admin/canned_replies', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_canned_replies():
    me = _current_user()
    if request.method == 'POST' and request.form.get('action') == 'signature':
        if not me:
            flash('The shared admin login has no signature. Sign in with your own account.', 'error')
        else:
            me.signature = request.form.get('signature', '').strip()[:1000] or None
            db.session.commit()
            flash('Signature saved. It\'s added under replies you email to requesters.', 'success')
        return redirect(url_for('admin_canned_replies'))
    replies = _visible_replies().order_by(CannedReply.title).all()
    return render_template('admin_canned_replies.html', replies=replies, me=me,
                           categories=TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all())


def _reply_from_form(reply):
    reply.title = request.form.get('title', '').strip()[:120]
    reply.body = request.form.get('body', '').strip()
    reply.category_id = request.form.get('category_id', type=int) or None
    me = _current_user()
    reply.user_id = me.id if (me and request.form.get('personal') == 'on') else None
    if not reply.title or not reply.body:
        raise ValueError('Give the reply a title and some text.')


@app.route('/admin/canned_replies/new', methods=['POST'])
@require_permission('tickets')
def admin_canned_reply_new():
    reply = CannedReply()
    try:
        _reply_from_form(reply)
        db.session.add(reply)
        _log_activity('canned_reply', f'Added saved reply "{reply.title}".')
        db.session.commit()
        flash(f'Added "{reply.title}".', 'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_canned_replies'))


@app.route('/admin/canned_replies/<int:reply_id>', methods=['POST'])
@require_permission('tickets')
def admin_canned_reply_edit(reply_id):
    reply = _visible_replies().filter(CannedReply.id == reply_id).first_or_404()
    if request.form.get('action') == 'delete':
        db.session.delete(reply)
        _log_activity('canned_reply', f'Deleted saved reply "{reply.title}".')
        db.session.commit()
        flash(f'Deleted "{reply.title}".', 'success')
        return redirect(url_for('admin_canned_replies'))
    try:
        _reply_from_form(reply)
        db.session.commit()
        flash(f'Saved "{reply.title}".', 'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_canned_replies'))


@app.route('/admin/tickets/<int:ticket_id>/canned/<int:reply_id>')
@require_permission('tickets')
def admin_ticket_canned(ticket_id, reply_id):
    """The saved reply's text filled in for this ticket (the reply box inserts it)."""
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    reply = _visible_replies().filter(CannedReply.id == reply_id).first_or_404()
    reply.use_count = (reply.use_count or 0) + 1
    db.session.commit()
    me = _current_user()
    return jsonify(text=render_canned(reply.body, ticket, me.username if me else 'IT'))


# ─── teams ────────────────────────────────────────────────────────────────────

@app.route('/admin/teams', methods=['GET', 'POST'])
@require_permission('admin')
def admin_teams():
    site_ids = _current_site_ids()
    techs = _ticket_assignees(site_ids)
    if request.method == 'POST':
        team = db.session.get(Team, request.form.get('team_id', type=int)) if request.form.get('team_id') else Team()
        if request.form.get('action') == 'delete' and team.id:
            Ticket.query.filter_by(team_id=team.id).update({'team_id': None})
            db.session.delete(team)
            _log_activity('team', f'Deleted team "{team.name}" (its tickets keep their assignee).')
            db.session.commit()
            flash(f'Deleted "{team.name}".', 'success')
            return redirect(url_for('admin_teams'))
        name = request.form.get('name', '').strip()[:80]
        if not name:
            flash('Give the team a name.', 'error')
            return redirect(url_for('admin_teams'))
        clash = Team.query.filter(db.func.lower(Team.name) == name.lower(), Team.id != (team.id or 0)).first()
        if clash:
            flash(f'There\'s already a team called "{clash.name}".', 'error')
            return redirect(url_for('admin_teams'))
        team.name = name
        site_id = request.form.get('site_id', type=int)
        team.site_id = site_id if site_id and db.session.get(Site, site_id) else None
        allowed = {u.id for u in techs}
        team.members = User.query.filter(User.id.in_([i for i in request.form.getlist('member_ids', type=int) if i in allowed])).all()
        db.session.add(team)
        _log_activity('team', f'Saved team "{team.name}" ({len(team.members)} member(s)).')
        db.session.commit()
        flash(f'Saved "{team.name}".', 'success')
        return redirect(url_for('admin_teams'))
    teams = Team.query.order_by(Team.name).all()
    open_counts = dict(db.session.query(Ticket.team_id, db.func.count(Ticket.id))
                       .filter(Ticket.team_id.isnot(None), Ticket.status.in_(['open', 'in_progress']))
                       .group_by(Ticket.team_id).all())
    return render_template('admin_teams.html', teams=teams, techs=techs, open_counts=open_counts,
                           sites=Site.query.order_by(Site.name).all())


# ─── presence ─────────────────────────────────────────────────────────────────

@app.route('/admin/tickets/<int:ticket_id>/presence', methods=['POST'])
@require_permission('tickets')
def admin_ticket_presence(ticket_id):
    """Heartbeat from an open ticket page; answers with who else is on it."""
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    _, label, user_id = _current_actor()
    key = f'user:{user_id}' if user_id else f'session:{session.get("presence_key") or _session_key()}'
    now = utcnow()
    row = TicketPresence.query.filter_by(ticket_id=ticket.id, actor_key=key).first()
    if row is None:
        row = TicketPresence(ticket_id=ticket.id, actor_key=key, actor_label=label)
        db.session.add(row)
    leaving = request.form.get('leaving') == '1'
    if leaving:
        db.session.delete(row)
    else:
        row.typing, row.updated_at, row.actor_label = request.form.get('typing') == '1', now, label
    TicketPresence.query.filter(TicketPresence.updated_at < now - timedelta(minutes=10)).delete()
    db.session.commit()
    others = (TicketPresence.query.filter(TicketPresence.ticket_id == ticket.id, TicketPresence.actor_key != key,
                                          TicketPresence.updated_at >= now - timedelta(seconds=PRESENCE_SECONDS))
              .order_by(TicketPresence.actor_label).all())
    return jsonify(others=[dict(name=o.actor_label, typing=o.typing) for o in others])


def _session_key():
    import secrets
    session['presence_key'] = secrets.token_hex(8)
    return session['presence_key']


# ─── merging and linking ──────────────────────────────────────────────────────

def _scoped_ticket(number):
    """A ticket the current user can see, from '123' or '#123', or None."""
    digits = str(number or '').strip().lstrip('#').strip()
    if not digits.isdigit():
        return None
    return _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=int(digits)).first()


def _reply_with_signature(body):
    me = _current_user()
    return f'{body}\n\n-- \n{me.signature}' if me and me.signature else body


@app.route('/admin/tickets/<int:ticket_id>/merge', methods=['POST'])
@require_permission('tickets')
def admin_ticket_merge(ticket_id):
    from foxdesk.core import EMAIL_ENABLED
    from foxdesk.services.helpdesk import _notify_ticket_requester
    from foxdesk.services.ticket_links import LinkError, final_ticket, merge_tickets
    this = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    other = _scoped_ticket(request.form.get('other'))
    if other is None:
        flash('Enter the number of a ticket you can see.', 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    if request.form.get('direction') == 'from':
        duplicate, keep = other, this
    else:
        duplicate, keep = this, final_ticket(other)
    _, actor_label, _ = _current_actor()
    try:
        summary = merge_tickets(duplicate, keep, actor_label)
        db.session.commit()
    except LinkError as e:
        db.session.rollback()
        flash(str(e), 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    told = ''
    if request.form.get('notify') == 'on' and EMAIL_ENABLED and duplicate.requester_email:
        body = (f'Thanks for letting us know. We\'re already working on this as ticket #{keep.id} '
                f'("{keep.subject}"), so we\'ve combined your request #{duplicate.id} with it. '
                f'A reply to this email still reaches us.')
        _notify_ticket_requester(duplicate, 'ticket_reply', {'tech_name': actor_label, 'reply_body': _reply_with_signature(body)},
                                 force=True)
        told = f' {duplicate.requester_email} was told.'
    flash(summary + told, 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=keep.id))


@app.route('/admin/tickets/<int:ticket_id>/link', methods=['POST'])
@require_permission('tickets')
def admin_ticket_link(ticket_id):
    from foxdesk.services.ticket_links import LinkError, final_ticket, link_ticket
    this = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    main = _scoped_ticket(request.form.get('main'))
    if main is None:
        flash('Enter the number of a ticket you can see.', 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    try:
        main = link_ticket(this, final_ticket(main))
        db.session.commit()
    except LinkError as e:
        db.session.rollback()
        flash(str(e), 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    flash(f'Ticket #{this.id} is now linked under #{main.id}.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/unlink', methods=['POST'])
@require_permission('tickets')
def admin_ticket_unlink(ticket_id):
    from foxdesk.services.ticket_links import unlink_ticket
    this = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    if this.parent_id:
        unlink_ticket(this)
        db.session.commit()
        flash(f'Ticket #{this.id} is no longer linked.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/resolve_linked', methods=['POST'])
@require_permission('tickets')
def admin_ticket_resolve_linked(ticket_id):
    """Resolve every open ticket linked under this one with one note, and
    optionally this ticket too."""
    from foxdesk.automation.engine import emit
    from foxdesk.core import EMAIL_ENABLED
    from foxdesk.services.helpdesk import _notify_ticket_requester
    from foxdesk.services.sla import mark_responded
    from foxdesk.services.ticket_links import resolve_linked
    main = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    message = request.form.get('message', '').strip()
    email_them = request.form.get('email_requesters') == 'on' and EMAIL_ENABLED
    _, actor_label, _ = _current_actor()
    resolved, to_email = resolve_linked(main, message, email_them, actor_label)
    main_emailed = False
    if request.form.get('include_main') == 'on' and main.is_open:
        resolved.append((main, main.status))
        main_emailed = bool(email_them and message and main.requester_email)
        if message:
            from foxdesk.models import TicketComment
            db.session.add(TicketComment(ticket_id=main.id, body=message, author_label=actor_label,
                                         emailed_to_requester=main_emailed))
        main.status, main.resolved_at, main.updated_at = 'resolved', utcnow(), utcnow()
        mark_responded(main)
        _log_activity('ticket_status', f'Ticket #{main.id} resolved with its linked tickets.', site_id=main.site_id, ticket_id=main.id)
        if main_emailed:
            to_email.append(main)
    db.session.commit()
    for t in to_email:
        _notify_ticket_requester(t, 'ticket_reply', {'tech_name': actor_label, 'reply_body': _reply_with_signature(message)},
                                 force=True)
    for t, old_status in resolved:
        if t not in to_email:
            _notify_ticket_requester(t, 'ticket_resolved')
        emit('ticket.status_changed', t, old_status=old_status)
    if not resolved:
        flash('Nothing to resolve — the linked tickets are already closed.', 'info')
    else:
        flash(f'Resolved {len(resolved)} ticket(s)' + (f'; {len(to_email)} requester(s) emailed.' if to_email else '.'), 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
