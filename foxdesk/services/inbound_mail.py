"""Email to ticket: read the help desk mailbox, turn new mail into tickets and
replies into comments on the ticket they answer.

Two ways to read the mailbox: Microsoft 365 through Microsoft Graph (an app
registration with Mail.ReadWrite; IMAP passwords don't work with M365 any
more) or plain IMAP over SSL (Google Workspace with an app password, others).

Threading: outgoing ticket emails carry "Ticket #123" in the subject, the
help desk address as Reply-To, and a Message-ID containing the ticket
number, so a reply finds its ticket by In-Reply-To/References first and the
subject second. A reply is only added to a ticket when it comes from the
requester (or a FoxDesk user); from anyone else it becomes a new ticket,
so someone can't post into another person's ticket by copying the subject.

Every message is claimed in InboundEmail (unique message id) before it's
handled, so it's never processed twice, even with several workers checking.
"""
import base64
import email
import email.policy
import hashlib
import html
import imaplib
import re
import uuid
from datetime import datetime, timedelta
from email.utils import getaddresses, parseaddr

import requests
from sqlalchemy.exc import IntegrityError

from foxdesk.core import SMTP_FROM_EMAIL, db, logger
from foxdesk.models import InboundEmail, MailboxSettings, Ticket, TicketCategory, TicketComment, User
from foxdesk.services.secrets import decrypt_secret, encrypt_secret

SECRET_PURPOSE = 'mailbox-secret'
GRAPH = 'https://graph.microsoft.com/v1.0'
BATCH = 25
MAX_TEXT = 20000
MAX_NEW_PER_SENDER_PER_HOUR = 10
TICKET_SUBJECT = re.compile(r'\bticket\s*#\s*(\d+)', re.IGNORECASE)
TICKET_MESSAGE_ID = re.compile(r'foxdesk-ticket-(\d+)-', re.IGNORECASE)
QUOTE_START = re.compile(r'^(On .{4,200}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|_{10,}\s*$|From:\s.+$)',
                         re.IGNORECASE | re.MULTILINE)


class MailboxError(Exception):
    """A mailbox problem, worded for the admin."""


def mailbox_settings():
    row = MailboxSettings.query.get(1)
    if row is None:
        row = MailboxSettings(id=1, enabled=False)
        db.session.add(row)
        db.session.flush()
    return row


def mailbox_configured(row=None):
    row = row or MailboxSettings.query.get(1)
    if not row or not row.address:
        return False
    if row.kind == 'graph':
        return bool(row.graph_tenant and row.graph_client_id and row.graph_client_secret)
    if row.kind == 'imap':
        return bool(row.imap_host and row.imap_user and row.imap_password)
    return False


def save_mailbox(row, form):
    row.kind = form.get('kind') if form.get('kind') in ('graph', 'imap') else None
    row.address = (form.get('address') or '').strip().lower() or None
    if row.address and '@' not in row.address:
        raise ValueError('Enter the help desk mailbox\'s email address.')
    row.imap_host = (form.get('imap_host') or '').strip() or None
    port = (form.get('imap_port') or '').strip()
    row.imap_port = int(port) if port.isdigit() else None
    row.imap_user = (form.get('imap_user') or '').strip() or None
    if form.get('imap_password'):
        row.imap_password = encrypt_secret(form['imap_password'], SECRET_PURPOSE)
    row.graph_tenant = (form.get('graph_tenant') or '').strip() or None
    row.graph_client_id = (form.get('graph_client_id') or '').strip() or None
    if form.get('graph_client_secret'):
        row.graph_client_secret = encrypt_secret(form['graph_client_secret'], SECRET_PURPOSE)
    category_id = form.get('default_category_id')
    row.default_category_id = int(category_id) if str(category_id or '').isdigit() else None
    row.enabled = form.get('enabled') == 'on'


# ─── outgoing: so replies come back to the right ticket ──────────────────────

def reply_headers(ticket_id):
    """(reply_to, extra headers) for a ticket email, or (None, {}) when the
    help desk mailbox isn't in use."""
    row = MailboxSettings.query.get(1)
    if not (row and row.enabled and row.address):
        return None, {}
    domain = row.address.split('@')[-1]
    return row.address, {'Message-ID': f'<foxdesk-ticket-{ticket_id}-{uuid.uuid4().hex[:12]}@{domain}>'}


# ─── reading the mailbox ─────────────────────────────────────────────────────

def _html_to_text(value):
    value = re.sub(r'(?is)<(script|style).*?</\1>', '', value)
    value = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</li>', '\n', value)
    value = re.sub(r'<[^>]+>', '', value)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(value)).strip()


def _is_auto(headers, subject):
    auto = (headers.get('auto-submitted') or 'no').lower()
    precedence = (headers.get('precedence') or '').lower()
    return (auto != 'no' or precedence in ('bulk', 'junk', 'list') or 'x-autoreply' in headers
            or bool(re.match(r'(automatic reply|auto(matic)?[- ]reply|out of (the )?office)', subject or '', re.IGNORECASE)))


def parse_raw(raw, uid=None):
    """An RFC 822 message (IMAP) as the plain dict process_message takes."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = {k.lower(): str(v) for k, v in msg.items()}
    name, addr = parseaddr(str(msg.get('From', '')))
    text = ''
    plain = msg.get_body(preferencelist=('plain',))
    if plain is not None:
        text = plain.get_content().strip()
    if not text:  # HTML-only mail, or an empty plain part next to the real HTML one
        rich = msg.get_body(preferencelist=('html',))
        text = _html_to_text(rich.get_content()) if rich is not None else ''
    attachments = []
    for part in msg.iter_attachments():
        try:
            attachments.append((part.get_filename() or 'attachment', part.get_content()))
        except Exception:
            continue
    message_id = (msg.get('Message-ID') or '').strip()
    return dict(uid=uid, message_id=message_id or None, from_email=addr.lower() or None, from_name=name or None,
                subject=str(msg.get('Subject') or '').strip(), text=text or '', in_reply_to=headers.get('in-reply-to', ''),
                references=headers.get('references', ''), cc=[a.lower() for _, a in getaddresses([headers.get('cc', '')]) if a],
                auto=_is_auto(headers, str(msg.get('Subject') or '')), attachments=attachments)


def _imap_fetch(row):
    password = decrypt_secret(row.imap_password, SECRET_PURPOSE) if row.imap_password else None
    if not password:
        raise MailboxError('The mailbox password can\'t be read. Enter it again.')
    try:
        conn = imaplib.IMAP4_SSL(row.imap_host, row.imap_port or 993, timeout=30)
        conn.login(row.imap_user, password)
        conn.select('INBOX')
        _, data = conn.search(None, 'UNSEEN')
        messages = []
        for num in (data[0].split() if data and data[0] else [])[:BATCH]:
            _, parts = conn.fetch(num, '(BODY.PEEK[])')
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                messages.append(parse_raw(raw, uid=num))
        return conn, messages
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailboxError(f'Couldn\'t read the mailbox over IMAP: {e}')


def _graph_token(row):
    secret = decrypt_secret(row.graph_client_secret, SECRET_PURPOSE) if row.graph_client_secret else None
    if not secret:
        raise MailboxError('The client secret can\'t be read. Enter it again.')
    r = requests.post(f'https://login.microsoftonline.com/{row.graph_tenant}/oauth2/v2.0/token',
                      data={'client_id': row.graph_client_id, 'client_secret': secret, 'grant_type': 'client_credentials',
                            'scope': 'https://graph.microsoft.com/.default'}, timeout=20)
    if r.status_code != 200:
        raise MailboxError('Microsoft rejected the app registration (check the tenant ID, client ID and secret).')
    return r.json()['access_token']


def _graph_get(token, url, **params):
    r = requests.get(url, params=params, timeout=30,
                     headers={'Authorization': f'Bearer {token}', 'Prefer': 'outlook.body-content-type="text"'})
    if r.status_code == 403:
        raise MailboxError('Microsoft refused access to the mailbox. The app needs the Mail.ReadWrite application '
                           'permission with admin consent (and, if you use one, the access policy must include this mailbox).')
    if r.status_code != 200:
        raise MailboxError(f'Microsoft Graph answered {r.status_code} reading the mailbox.')
    return r.json()


def _graph_fetch(row):
    token = _graph_token(row)
    box = f'{GRAPH}/users/{row.address}'
    data = _graph_get(token, f'{box}/mailFolders/inbox/messages', **{
        '$filter': 'isRead eq false', '$top': BATCH, '$orderby': 'receivedDateTime',
        '$select': 'id,subject,from,body,internetMessageId,internetMessageHeaders,hasAttachments,ccRecipients'})
    messages = []
    for item in data.get('value', []):
        headers = {h['name'].lower(): h['value'] for h in item.get('internetMessageHeaders') or []}
        sender = (item.get('from') or {}).get('emailAddress') or {}
        body = item.get('body') or {}
        text = body.get('content') or ''
        if body.get('contentType') == 'html':
            text = _html_to_text(text)
        attachments = []
        if item.get('hasAttachments'):
            for a in _graph_get(token, f'{box}/messages/{item["id"]}/attachments').get('value', []):
                if a.get('@odata.type') == '#microsoft.graph.fileAttachment' and a.get('contentBytes'):
                    attachments.append((a.get('name') or 'attachment', base64.b64decode(a['contentBytes'])))
        messages.append(dict(uid=item['id'], message_id=item.get('internetMessageId'),
                             from_email=(sender.get('address') or '').lower() or None, from_name=sender.get('name'),
                             subject=item.get('subject') or '', text=text, in_reply_to=headers.get('in-reply-to', ''),
                             references=headers.get('references', ''),
                             cc=[((c.get('emailAddress') or {}).get('address') or '').lower() for c in item.get('ccRecipients') or []],
                             auto=_is_auto(headers, item.get('subject') or ''), attachments=attachments))
    return token, messages


def _graph_mark_read(row, token, uid):
    requests.patch(f'{GRAPH}/users/{row.address}/messages/{uid}', json={'isRead': True}, timeout=20,
                   headers={'Authorization': f'Bearer {token}'})


def fetch_unread(row):
    """(handle, messages); handle is what mark_read needs."""
    if row.kind == 'graph':
        return _graph_fetch(row)
    if row.kind == 'imap':
        return _imap_fetch(row)
    raise MailboxError('Choose how to read the mailbox.')


def mark_read(row, handle, uid):
    if uid is None:
        return
    try:
        if row.kind == 'graph':
            _graph_mark_read(row, handle, uid)
        elif row.kind == 'imap':
            handle.store(uid, '+FLAGS', '\\Seen')
    except Exception as e:
        logger.warning('Could not mark message %s read: %s', uid, e)


def close(row, handle):
    if row.kind == 'imap' and handle is not None:
        try:
            handle.logout()
        except Exception:
            pass


# ─── turning a message into a ticket or a reply ──────────────────────────────

def strip_quoted(text):
    """The new part of a reply: everything above "On ... wrote:" and the like."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith('>'):
            break
        lines.append(line)
    trimmed = '\n'.join(lines)
    match = QUOTE_START.search(trimmed)
    if match and match.start() > 0:
        trimmed = trimmed[:match.start()]
    return trimmed.strip() or text.strip()


def ticket_reference(m):
    for header in (m.get('in_reply_to') or '', m.get('references') or ''):
        found = TICKET_MESSAGE_ID.search(header)
        if found:
            return int(found.group(1))
    found = TICKET_SUBJECT.search(m.get('subject') or '')
    return int(found.group(1)) if found else None


def _claim(m):
    key = (m.get('message_id') or '').strip()[:255] or 'sha:' + hashlib.sha256(
        f"{m.get('from_email')}|{m.get('subject')}|{(m.get('text') or '')[:500]}".encode()).hexdigest()
    row = InboundEmail(message_id=key, from_email=(m.get('from_email') or '')[:255] or None,
                       subject=(m.get('subject') or '')[:255] or None, outcome='pending')
    try:
        with db.session.begin_nested():
            db.session.add(row)
        return row
    except IntegrityError:
        return None


class _Upload:
    """Looks enough like an uploaded file for services/attachments."""
    def __init__(self, name, data):
        self.filename, self._data = name, data

    def read(self):
        return self._data


def _save_mail_attachments(ticket, m, who):
    from foxdesk.services.attachments import _save_attachments
    saved = 0
    for name, data in m.get('attachments') or []:
        try:
            saved += _save_attachments('ticket', ticket.id, [_Upload(name, data)], uploaded_by=who)
        except ValueError:
            continue  # not a photo/PDF, or too big: skipped, the message text still comes through
    return saved


def process_message(m, row):
    """Handle one message. Returns the InboundEmail row (None if already handled)."""
    from foxdesk.automation.engine import emit
    from foxdesk.services.auth import _log_activity
    from foxdesk.services.helpdesk import _create_ticket, _notify_ticket_requester
    from foxdesk.services.identities import find_person

    record = _claim(m)
    if record is None:
        return None
    sender = m.get('from_email')
    ours = {a for a in (row.address, (SMTP_FROM_EMAIL or '').lower()) if a}
    if not sender:
        record.outcome, record.detail = 'ignored', 'No sender address.'
    elif m.get('auto'):
        record.outcome, record.detail = 'ignored', 'Automatic reply or bulk mail.'
    elif sender in ours:
        record.outcome, record.detail = 'ignored', 'Sent by FoxDesk itself.'
    if record.outcome != 'pending':
        db.session.commit()
        return record

    who = m.get('from_name') or sender
    ticket = Ticket.query.get(ticket_reference(m)) if ticket_reference(m) else None
    staff = User.query.filter(db.func.lower(User.email) == sender, User.is_active.is_(True)).first()
    person = find_person(email=sender)
    allowed = ticket is not None and (
        sender == (ticket.requester_email or '').lower() or staff is not None
        or (person is not None and person.id == ticket.requester_person_id))
    if allowed:
        text = strip_quoted(m.get('text') or '')[:MAX_TEXT] or '(no message text)'
        db.session.add(TicketComment(ticket_id=ticket.id, body=text, author_label=f'{who} (email)',
                                     from_requester=staff is None))
        if staff is None and ticket.status in ('resolved', 'closed'):
            ticket.status, ticket.resolved_at = 'open', None
        ticket.updated_at = datetime.utcnow()
        count = _save_mail_attachments(ticket, m, who)
        _log_activity('ticket_comment', f'Email reply from {sender} on ticket #{ticket.id}'
                      + (f' with {count} attachment(s)' if count else '') + '.', site_id=ticket.site_id, ticket_id=ticket.id)
        record.outcome, record.ticket_id, record.detail = 'reply', ticket.id, f'Added to ticket #{ticket.id}.'
        db.session.commit()
        return record

    recent = InboundEmail.query.filter(InboundEmail.from_email == sender, InboundEmail.outcome == 'new',
                                       InboundEmail.received_at >= datetime.utcnow() - timedelta(hours=1)).count()
    if recent >= MAX_NEW_PER_SENDER_PER_HOUR:
        record.outcome, record.detail = 'ignored', 'Too many new tickets from this sender in the last hour (mail loop?).'
        db.session.commit()
        return record
    category = row.default_category or TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).first()
    if category is None:
        record.outcome, record.detail = 'error', 'There\'s no ticket category to file it under.'
        db.session.commit()
        return record
    subject = re.sub(r'^\s*((re|fw|fwd)\s*:\s*)+', '', m.get('subject') or '', flags=re.IGNORECASE).strip()[:200] or '(no subject)'
    ticket = _create_ticket(category.id, subject, (m.get('text') or '').strip()[:MAX_TEXT] or '(no message text)',
                            person=person, requester_name=who, requester_email=sender,
                            site_id=person.site_id if person else None)
    _save_mail_attachments(ticket, m, who)
    record.outcome, record.ticket_id, record.detail = 'new', ticket.id, f'Opened ticket #{ticket.id}.'
    db.session.commit()
    emit('ticket.created', ticket)
    _notify_ticket_requester(ticket, 'ticket_received')
    return record


def check_mailbox():
    """Read unread mail and handle it. Returns a short summary. Each message
    is marked read once handled; a failure on one doesn't stop the rest."""
    row = mailbox_settings()
    db.session.commit()
    if not (row.enabled and mailbox_configured(row)):
        return 'Not turned on.'
    handle = None
    counts = {'new': 0, 'reply': 0, 'ignored': 0, 'error': 0}
    try:
        handle, messages = fetch_unread(row)
        for m in messages:
            try:
                record = process_message(m, row)
                if record is not None:
                    counts[record.outcome] = counts.get(record.outcome, 0) + 1
            except Exception as e:
                db.session.rollback()
                logger.exception('Inbound email from %s failed: %s', m.get('from_email'), e)
                counts['error'] += 1
            mark_read(row, handle, m.get('uid'))
        summary = (f'{counts["new"]} new ticket(s), {counts["reply"]} repl(ies), {counts["ignored"]} ignored'
                   + (f', {counts["error"]} failed' if counts['error'] else ''))
        row = mailbox_settings()
        row.last_check_at, row.last_summary, row.last_error = datetime.utcnow(), summary[:255], None
        db.session.commit()
        return summary
    except MailboxError as e:
        db.session.rollback()
        row = mailbox_settings()
        row.last_check_at, row.last_error = datetime.utcnow(), str(e)
        db.session.commit()
        raise
    finally:
        close(row, handle)
