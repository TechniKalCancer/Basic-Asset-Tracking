"""Pages: Settings → Help desk email (email to ticket)."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import EMAIL_ENABLED, SMTP_FROM_EMAIL, app, db
from foxdesk.models import InboundEmail, TicketCategory
from foxdesk.services.auth import _log_activity, require_super_admin
from foxdesk.services.inbound_mail import (
    MailboxError, check_mailbox, close, fetch_unread, mailbox_configured, mailbox_settings, save_mailbox,
)


@app.route('/admin/helpdesk_email', methods=['GET', 'POST'])
@require_super_admin
def admin_helpdesk_email():
    row = mailbox_settings()
    if request.method == 'POST':
        try:
            save_mailbox(row, request.form)
            if row.enabled and not mailbox_configured(row):
                row.enabled = False
                flash('Saved, but left off: fill in every field for the method you chose first.', 'error')
            else:
                flash('Saved.' + (' New mail is checked every 2 minutes.' if row.enabled else ''), 'success')
            _log_activity('helpdesk_email', f'Saved the help desk mailbox settings ({row.address or "no address"}, '
                                            f'{"on" if row.enabled else "off"}).')
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
        return redirect(url_for('admin_helpdesk_email'))
    db.session.commit()
    recent = InboundEmail.query.order_by(InboundEmail.received_at.desc()).limit(50).all()
    return render_template('admin_helpdesk_email.html', row=row, configured=mailbox_configured(row), recent=recent,
                           categories=TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all(),
                           email_enabled=EMAIL_ENABLED, smtp_from=SMTP_FROM_EMAIL)


@app.route('/admin/helpdesk_email/test', methods=['POST'])
@require_super_admin
def admin_helpdesk_email_test():
    """Connect and count unread mail without handling anything."""
    row = mailbox_settings()
    handle = None
    try:
        handle, messages = fetch_unread(row)
        flash(f'Connected to {row.address}. {len(messages)} unread message(s) waiting'
              + (' (showing up to 25).' if len(messages) >= 25 else '.'), 'success')
    except MailboxError as e:
        flash(str(e), 'error')
    finally:
        close(row, handle)
    return redirect(url_for('admin_helpdesk_email'))


@app.route('/admin/helpdesk_email/check', methods=['POST'])
@require_super_admin
def admin_helpdesk_email_check():
    try:
        flash('Checked: ' + check_mailbox(), 'success')
    except MailboxError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_helpdesk_email'))
