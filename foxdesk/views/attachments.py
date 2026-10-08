"""Pages: attachments."""
from flask import abort, flash, redirect, request
from foxdesk.core import app, db
from foxdesk.models import Attachment
from foxdesk.services.auth import _current_actor, _log_activity, login_required
from foxdesk.services.attachments import _attachment_owner_in_scope, _attachment_return_url, _save_attachments


@app.route('/admin/attachments/<int:attachment_id>')
@login_required
def admin_attachment_view(attachment_id):
    attachment = Attachment.query.get_or_404(attachment_id)
    if not _attachment_owner_in_scope(attachment.owner_type, attachment.owner_id):
        abort(404)
    response = app.response_class(attachment.data, mimetype=attachment.content_type)
    disposition = 'attachment' if request.args.get('download') else 'inline'
    response.headers['Content-Disposition'] = f'{disposition}; filename="{attachment.filename}"'
    response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; img-src 'self'"
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response


@app.route('/admin/attachments/<string:owner_type>/<int:owner_id>', methods=['POST'])
@login_required
def admin_attachment_upload(owner_type, owner_id):
    owner = _attachment_owner_in_scope(owner_type, owner_id)
    if not owner:
        abort(404)
    _, actor_label, _ = _current_actor()
    try:
        count = _save_attachments(owner_type, owner_id, request.files.getlist('photos'), uploaded_by=actor_label)
        if count:
            _log_activity('attachment_add', f'Attached {count} file(s) to {owner_type} #{owner_id}.',
                           ticket_id=owner_id if owner_type == 'ticket' else None)
        db.session.commit()
        flash(f'Attached {count} file(s).' if count else 'Choose a photo first.', 'success' if count else 'error')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(_attachment_return_url(owner_type, owner))


@app.route('/admin/attachments/<int:attachment_id>/delete', methods=['POST'])
@login_required
def admin_attachment_delete(attachment_id):
    attachment = Attachment.query.get_or_404(attachment_id)
    owner = _attachment_owner_in_scope(attachment.owner_type, attachment.owner_id)
    if not owner:
        abort(404)
    try:
        _log_activity('attachment_delete', f'Removed attachment "{attachment.filename}" from '
                       f'{attachment.owner_type} #{attachment.owner_id}.',
                       ticket_id=attachment.owner_id if attachment.owner_type == 'ticket' else None)
        db.session.delete(attachment)
        db.session.commit()
        flash('Attachment removed.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not remove attachment: {e}', 'error')
    return redirect(_attachment_return_url(attachment.owner_type, owner))
