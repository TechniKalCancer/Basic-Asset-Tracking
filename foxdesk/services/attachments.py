"""Photo/PDF attachments: validation, storage, and scope checks."""
from collections import defaultdict
from flask import url_for
from werkzeug.utils import secure_filename
from foxdesk.core import db
from foxdesk.models import AssetRegistry, Attachment, Incident, Repair, Ticket
from foxdesk.services.auth import _current_site_ids, _has_permission
from foxdesk.services.scoping import _scope_registry, _scope_repairs, _scope_tickets


ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024  # per file, after client-side downscaling — generous headroom for a PDF


ATTACHMENT_MAX_PER_UPLOAD = 6


# Magic-byte sniffing, not the browser-supplied Content-Type or extension —
# both are trivially spoofable and these bytes get served back inline.
_ATTACHMENT_SIGNATURES = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'%PDF-', 'application/pdf'),
)


ATTACHMENT_PERMISSIONS = {'incident': 'devices', 'ticket': 'tickets', 'repair': 'repairs'}


def _sniff_attachment_type(data):
    for signature, content_type in _ATTACHMENT_SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _save_attachments(owner_type, owner_id, files, uploaded_by=None):
    """Stores each uploaded file in `files` (a request.files.getlist()) as an
    Attachment on the given owner. Silently skips empty file inputs; raises
    ValueError for a disallowed type or oversized file so the caller's
    try/except rolls back the whole action (a ticket shouldn't half-save
    with some photos missing). Does not commit. Returns the count saved."""
    files = [f for f in (files or []) if f and f.filename]
    if len(files) > ATTACHMENT_MAX_PER_UPLOAD:
        raise ValueError(f'At most {ATTACHMENT_MAX_PER_UPLOAD} files per upload.')
    for f in files:
        data = f.read()
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise ValueError(f'"{f.filename}" is larger than {ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB.')
        content_type = _sniff_attachment_type(data)
        if not content_type:
            raise ValueError(f'"{f.filename}" isn\'t a supported photo (JPEG/PNG/GIF/WebP) or PDF.')
        db.session.add(Attachment(
            owner_type=owner_type, owner_id=owner_id, filename=secure_filename(f.filename) or 'upload',
            content_type=content_type, size_bytes=len(data), data=data, uploaded_by=uploaded_by,
        ))
    return len(files)


def _attachments_for(owner_type, owner_ids):
    """{owner_id: [Attachment, ...]} for a batch of owners — one query for a
    whole table of incidents instead of one per row."""
    owner_ids = list(owner_ids)
    grouped = defaultdict(list)
    if owner_ids:
        rows = Attachment.query.filter(Attachment.owner_type == owner_type, Attachment.owner_id.in_(owner_ids)) \
            .order_by(Attachment.created_at).all()
        for row in rows:
            grouped[row.owner_id].append(row)
    return grouped


def _attachment_owner_in_scope(owner_type, owner_id):
    """Returns the owning Incident/Ticket/Repair if the current user has
    the matching permission AND it's within their site scope, else None."""
    if owner_type not in ATTACHMENT_PERMISSIONS or not _has_permission(ATTACHMENT_PERMISSIONS[owner_type]):
        return None
    site_ids = _current_site_ids()
    if owner_type == 'ticket':
        return _scope_tickets(Ticket.query, site_ids).filter_by(id=owner_id).first()
    if owner_type == 'repair':
        return _scope_repairs(Repair.query, site_ids).filter_by(id=owner_id).first()
    incident = Incident.query.get(owner_id)
    if incident and site_ids is not None:
        in_scope = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=incident.asset_tag).first()
        return incident if in_scope else None
    return incident


def _attachment_return_url(owner_type, owner):
    if owner_type == 'ticket':
        return url_for('admin_ticket_detail', ticket_id=owner.id)
    if owner_type == 'repair':
        return url_for('admin_repair_detail', repair_id=owner.id)
    return url_for('admin_asset_assign', asset_tag=owner.asset_tag)
