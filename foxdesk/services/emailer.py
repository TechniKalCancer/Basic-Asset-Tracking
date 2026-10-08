"""Outgoing email: SMTP send, background send, and admin-editable templates."""
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage
from foxdesk.core import (
    EMAIL_ENABLED,
    SMTP_FROM_EMAIL,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SERVER,
    SMTP_USERNAME,
    db,
    logger,
)
from foxdesk.models import EmailSettings


def send_email(to_email, subject, body):
    """
    Sends a plain-text email via SMTP (Gmail by default: smtp.gmail.com:587 with
    an App Password — a regular account password will not work with 2FA enabled).
    Also works against an IP-allowlisted Google Workspace SMTP relay
    (smtp-relay.gmail.com) with no SMTP_USERNAME/SMTP_PASSWORD set at all —
    login() is only attempted when both are present.

    Args:
        to_email: Recipient address.
        subject: Email subject line.
        body: Plain-text email body.

    Raises:
        RuntimeError: If SMTP_FROM_EMAIL is not configured.
        smtplib.SMTPException, OSError: On connection/authentication/send failure.
    """
    if not EMAIL_ENABLED:
        raise RuntimeError('Email is not configured (set SMTP_FROM_EMAIL in .env).')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM_EMAIL
    msg['To'] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
        server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def _send_email_in_background(to_email, subject, body):
    """Fire-and-forget send_email() on a daemon thread, for emails triggered
    as a side effect of a page action (ticket submitted, status changed) —
    a slow or unreachable SMTP server must never make a student's form
    submission hang for 10s or fail. Failures are only logged."""
    def _run():
        try:
            send_email(to_email, subject, body)
            logger.info('Sent "%s" email to %s', subject, to_email)
        except Exception as e:
            logger.error('Background email to %s failed: %s', to_email, e)
    threading.Thread(target=_run, daemon=True).start()


# ─── Customizable email wording ────────────────────────────────────────────────
# Every system email this app sends is registered here as a "kind" with a
# built-in default subject/body. An admin can override either at
# /admin/emails (stored on EmailSettings); _render_email_template() applies
# the override if set, otherwise falls back to the default below. Keeping
# the defaults here (not hardcoded inline at each send_email() call site)
# means the customization UI and the actual send path can never drift apart.
EMAIL_TEMPLATE_KINDS = {
    'loaner_overdue': {
        'label': 'Loaner Reminder — Overdue',
        'subject': 'Reminder: loaner {asset_tag} is overdue for return',
        'body': (
            'Hi {first_name},\n\n'
            'Our records show loaner device {asset_tag} was due back on '
            '{due_date} ({days_overdue} day{days_overdue_plural} ago).\n\n'
            'Please return it to the office as soon as possible. If you\'ve already returned it, '
            'this reminder can be ignored.\n\nThanks!'
        ),
    },
    'loaner_upcoming': {
        'label': 'Loaner Reminder — Due Soon',
        'subject': 'Reminder: loaner {asset_tag} is due back {due_date}',
        'body': (
            'Hi {first_name},\n\n'
            'Just a reminder that loaner device {asset_tag} is due back on {due_date}.\n\n'
            'Please return it to the office by then. If you\'ve already returned it, '
            'this reminder can be ignored.\n\nThanks!'
        ),
    },
    'loaner_nodate': {
        'label': 'Loaner Reminder — No Due Date Set',
        'subject': 'Reminder: please return loaner {asset_tag}',
        'body': (
            'Hi {first_name},\n\n'
            'Just a reminder that you currently have loaner device {asset_tag} checked out. '
            'Please return it to the office when you\'re done with it.\n\n'
            'If you\'ve already returned it, this reminder can be ignored.\n\nThanks!'
        ),
    },
    'assignment_overdue': {
        'label': 'Assigned Device Reminder — Overdue',
        'subject': 'Reminder: {asset_tag} is overdue for return',
        'body': (
            'Hi {first_name},\n\n'
            'Our records show asset {asset_tag} was due back on '
            '{due_date} ({days_overdue} day{days_overdue_plural} ago).\n\n'
            'Please return it as soon as possible. If you\'ve already returned it, this reminder can be ignored.\n\n'
            'Thanks!'
        ),
    },
    'ticket_received': {
        'label': 'Ticket — Received (to requester)',
        'subject': 'We got your request: {ticket_subject} [Ticket #{ticket_id}]',
        'body': (
            'Hi {first_name},\n\n'
            'Thanks for reaching out — your ticket #{ticket_id} ("{ticket_subject}") has been received '
            'and our tech team will take a look.\n\n'
            'What you told us:\n{ticket_description}\n\n'
            'You\'ll get another email when it\'s resolved.\n\nThanks!'
        ),
    },
    'ticket_reply': {
        'label': 'Ticket — Reply from a Tech (to requester)',
        'subject': 'Update on your ticket #{ticket_id}: {ticket_subject}',
        'body': (
            'Hi {first_name},\n\n'
            '{tech_name} posted an update on your ticket #{ticket_id} ("{ticket_subject}"):\n\n'
            '{reply_body}\n\n'
            'Thanks!'
        ),
    },
    'ticket_resolved': {
        'label': 'Ticket — Resolved (to requester)',
        'subject': 'Resolved: {ticket_subject} [Ticket #{ticket_id}]',
        'body': (
            'Hi {first_name},\n\n'
            'Your ticket #{ticket_id} ("{ticket_subject}") has been marked {ticket_status}.\n\n'
            'If the problem isn\'t fixed, just submit a new ticket or stop by the tech office.\n\nThanks!'
        ),
    },
    'damage_notice': {
        'label': 'Damage Notice (to parent/guardian)',
        'subject': 'Device damage report for {student_name}',
        'body': (
            'Dear {guardian_name},\n\n'
            'This is to let you know that a damage/loss report was logged on {incident_date} for the '
            'school device assigned to {student_name} (asset tag {asset_tag}):\n\n'
            '{incident_description}\n\n'
            '{fee_line}\n\n'
            'Please contact the school office with any questions.\n\nThank you.'
        ),
    },
}


EMAIL_TEMPLATE_VARIABLES = {
    'loaner_overdue': ['first_name', 'full_name', 'asset_tag', 'due_date', 'days_overdue'],
    'loaner_upcoming': ['first_name', 'full_name', 'asset_tag', 'due_date'],
    'loaner_nodate': ['first_name', 'full_name', 'asset_tag'],
    'assignment_overdue': ['first_name', 'full_name', 'asset_tag', 'due_date', 'days_overdue'],
    'ticket_received': ['first_name', 'full_name', 'ticket_id', 'ticket_subject', 'ticket_description'],
    'ticket_reply': ['first_name', 'full_name', 'ticket_id', 'ticket_subject', 'tech_name', 'reply_body'],
    'ticket_resolved': ['first_name', 'full_name', 'ticket_id', 'ticket_subject', 'ticket_status'],
    'damage_notice': ['guardian_name', 'student_name', 'asset_tag', 'incident_date', 'incident_description', 'fee_line'],
}


class _SafeFormatDict(dict):
    """Used with str.format_map() so a template referencing an unknown or
    misspelled variable name renders the literal {placeholder} instead of
    raising KeyError — a typo in a saved template can't break email sending."""
    def __missing__(self, key):
        return '{' + key + '}'


def _get_email_settings():
    settings = EmailSettings.query.get(1)
    if not settings:
        settings = EmailSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


def _render_email_template(kind, variables):
    """Renders the subject/body for `kind` — the admin-customized template
    if one is saved, otherwise the built-in default. variables is a dict of
    already-display-formatted strings (e.g. due_date as 'YYYY-MM-DD'). Falls
    back to rendering the built-in default if a saved custom template has
    become malformed (e.g. an unclosed brace), so a bad template can never
    crash a reminder send — /admin/emails also validates on save to catch
    this before it's ever stored."""
    settings = EmailSettings.query.get(1)
    default = EMAIL_TEMPLATE_KINDS[kind]
    subject_tpl = (getattr(settings, f'{kind}_subject', None) if settings else None) or default['subject']
    body_tpl = (getattr(settings, f'{kind}_body', None) if settings else None) or default['body']
    safe_vars = _SafeFormatDict(variables)
    try:
        return subject_tpl.format_map(safe_vars), body_tpl.format_map(safe_vars)
    except (ValueError, IndexError) as e:
        logger.error('Malformed custom email template for %s, falling back to default: %s', kind, e)
        return default['subject'].format_map(safe_vars), default['body'].format_map(safe_vars)


def _email_template_sample_vars():
    """Fake-but-realistic values used to validate a saved template and to
    render the live preview on /admin/emails — computed fresh per request
    so the sample due date always reads as "today", not whenever the
    server process happened to start."""
    return {
        'first_name': 'Jordan', 'full_name': 'Jordan Smith', 'asset_tag': '123456',
        'due_date': datetime.utcnow().date().isoformat(), 'days_overdue': '3', 'days_overdue_plural': 's',
        'ticket_id': '1042', 'ticket_subject': 'Chromebook won\'t charge',
        'ticket_description': 'The charging light doesn\'t come on with any charger I try.',
        'ticket_status': 'resolved', 'tech_name': 'Tech Office',
        'reply_body': 'We swapped the charging port — you can pick it up from the library.',
        'guardian_name': 'Pat Smith', 'student_name': 'Jordan Smith',
        'incident_date': datetime.utcnow().date().isoformat(),
        'incident_description': 'Cracked screen', 'fee_line': 'A repair fee of $45.00 has been assessed.',
    }
