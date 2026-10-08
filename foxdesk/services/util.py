"""Small parsing and lookup helpers shared across areas."""
import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from foxdesk.core import db
from foxdesk.models import Asset, AssetNumberRange, AssetRegistry


def _generate_asset_tag(existing_tags):
    """
    Self-assigns a tag when one isn't provided, whether adding a device
    manually or importing a CSV row with no asset_tag/serial.

    If a default AssetNumberRange is set, pulls the next sequential number
    from it (same as picking that range by hand on Add Device) — this is
    what makes a CSV import of a whole batch of pre-printed labels "just
    work" without anyone selecting a range per row. Falls through to the
    old whole-space random behavior (still avoiding every reserved range,
    default or not) if there's no default range or it's fully used, so a
    bulk import never hard-fails partway through just because one batch
    ran out.
    """
    default_range = AssetNumberRange.query.filter_by(is_default=True).first()
    if default_range:
        tag = _next_tag_in_range(existing_tags, default_range.range_start, default_range.range_end)
        if tag:
            return tag

    reserved = [(r.range_start, r.range_end) for r in AssetNumberRange.query.all()]
    for _ in range(50):
        candidate = secrets.randbelow(900000) + 100000
        if str(candidate) in existing_tags:
            continue
        if any(start <= candidate <= end for start, end in reserved):
            continue
        return str(candidate)
    raise RuntimeError('Could not generate a unique asset tag after 50 attempts.')


def _next_tag_in_range(existing_tags, range_start, range_end):
    """
    Returns the lowest unused number in [range_start, range_end] as a string,
    or None if every number in the range is already taken. Used when an admin
    deliberately picks a reserved AssetNumberRange to draw from on Add Device
    — unlike _generate_asset_tag's random pick avoiding every reserved range,
    this pulls sequentially from inside ONE chosen range, matching a physical
    batch of pre-printed labels (grab the next sticker in the stack).
    """
    for candidate in range(range_start, range_end + 1):
        if str(candidate) not in existing_tags:
            return str(candidate)
    return None


def resolve_scan(scanned_value: str):
    """
    Given a raw scan value, return (asset_tag, scan_type) or (None, None).
    Checks asset_tag first, then serial_number.
    """
    scanned_value = scanned_value.strip()
    row = AssetRegistry.query.filter_by(asset_tag=scanned_value).first()
    if row:
        return row.asset_tag, 'asset_tag'
    row = AssetRegistry.query.filter_by(serial_number=scanned_value).first()
    if row:
        return row.asset_tag, 'serial'
    return None, None


def heal_orphans():
    """
    After a CSV import, mark previously-invalid Asset records as valid
    if their asset_tag now exists in the registry.
    """
    orphans = Asset.query.filter_by(is_valid=False).all()
    healed = 0
    for asset in orphans:
        if AssetRegistry.query.filter_by(asset_tag=asset.asset_tag).first():
            asset.is_valid = True
            healed += 1
    if healed:
        db.session.commit()
    return healed


def _get_nested_value(data, dotted_path):
    """Resolves a dotted path like 'name.givenName' or 'phones.0.value'
    against a nested dict/list (as returned by the Google API). Returns
    None if any segment along the way is missing, rather than raising —
    a mapping referencing a field a given record just doesn't have is a
    normal, expected case (e.g. not everyone has a phones entry)."""
    current = data
    for part in dotted_path.split('.'):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _top_n_with_other(counter, n=8):
    """[(label, value), ...] sorted desc, with everything past n folded into
    one "Other" row — a 15-model fleet shouldn't become a 15-bar chart."""
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    head, tail = ranked[:n], ranked[n:]
    if tail:
        head.append(('Other', sum(v for _, v in tail)))
    return head


def _parse_date(value):
    """Parses a 'YYYY-MM-DD' form field into a date, or None if blank/invalid."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _parse_money(value):
    """Parses a dollar-amount form field into a Decimal, or None if blank/invalid/negative."""
    value = (value or '').strip().lstrip('$')
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _parse_bool_csv(value):
    """Parses a lenient boolean CSV cell. Returns True/False, or None if the
    cell is blank/missing — None means 'leave the existing value alone' on an
    upsert, distinct from an explicit false which clears a previously-true flag."""
    value = (value or '').strip().lower()
    if not value:
        return None
    return value in ('true', 'yes', 'y', '1')


def _slugify_field_key(label):
    """Turns a human label like 'Employee ID' into a safe JSON-key/slug like
    'employee_id' — lowercase, non-alphanumerics collapsed to underscores,
    trimmed. Used so custom fields don't need a separate raw-key input."""
    slug = re.sub(r'[^a-z0-9]+', '_', label.strip().lower()).strip('_')
    return slug or 'field'
