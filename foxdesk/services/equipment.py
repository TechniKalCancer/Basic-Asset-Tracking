"""Equipment — servers, network gear, printers, displays — and the
locations (buildings, rooms, closets, racks, carts) it lives in.

Equipment is an ordinary registry device with an equipment type, so
repairs, tickets, labels, warranties and linked records all work the same;
what differs is that it's placed in a location rather than checked out to
someone, and it carries how to reach it (hostname, IP, firmware) and how
long it's supported.
"""
from datetime import date

from foxdesk.core import app, db
from foxdesk.models import DEVICE_TYPE_LABELS, EQUIPMENT_TYPES, AssetRegistry, Location


def type_label(device_type, plural=False):
    one, many = DEVICE_TYPE_LABELS.get(device_type, ((device_type or '').replace('_', ' ').capitalize(),) * 2)
    return many if plural else one


def equipment_query(base=None):
    return (base if base is not None else AssetRegistry.query).filter(AssetRegistry.device_type.in_(EQUIPMENT_TYPES))


def support_days_left(row, today=None):
    """Days until the earlier of its support contract or the maker's end
    of support (negative once past), or None if neither is known."""
    dates = [d for d in (row.support_ends, row.end_of_support) if d]
    return (min(dates) - (today or date.today())).days if dates else None


def support_status(row, today=None):
    """dict(text, css) for the support column, or None."""
    days = support_days_left(row, today)
    if days is None:
        return None
    when = min(d for d in (row.support_ends, row.end_of_support) if d)
    if days < 0:
        return dict(text=f'Unsupported since {when:%b %Y}', css='badge-red')
    if days <= 180:
        return dict(text=f'Support ends {when:%b %d, %Y}', css='badge-yellow')
    return dict(text=f'Supported to {when:%b %Y}', css='badge-neutral')


def monitor_status(row):
    """What LibreNMS last said about it: dict(up, seen) or None."""
    rec = next((r for r in row.records if r.source == 'librenms'), None) if row.id else None
    if rec is None:
        return None
    return dict(up=bool((rec.raw or {}).get('up')), seen=rec.last_seen_at, device_id=rec.external_key)


# ─── locations ────────────────────────────────────────────────────────────────

def locations_for(site_ids=None):
    """Every location (optionally only at these schools), parents before
    children, as (location, depth) for indented pickers and lists."""
    query = Location.query
    if site_ids is not None:
        query = query.filter(Location.site_id.in_(site_ids or [-1]))
    rows = query.all()
    by_parent = {}
    for loc in rows:
        by_parent.setdefault(loc.parent_id, []).append(loc)
    ids = {loc.id for loc in rows}
    out = []

    def walk(parent_id, depth):
        for loc in sorted(by_parent.get(parent_id, []), key=lambda l: (l.site.name if l.site else '', l.name.lower())):
            out.append((loc, depth))
            walk(loc.id, depth + 1)
    walk(None, 0)
    for loc in rows:  # a child whose parent is out of scope still shows, at the top
        if loc.parent_id and loc.parent_id not in ids:
            out.append((loc, 0))
            walk(loc.id, 1)
    return out


def find_or_create_location(site_id, name, kind='room', parent=None):
    """The location with this name at this school (and under this parent),
    made if it doesn't exist. Does not commit."""
    name = (name or '').strip()[:120]
    if not name or not site_id:
        return None
    same_parent = Location.parent_id == parent.id if parent else Location.parent_id.is_(None)
    loc = Location.query.filter(Location.site_id == site_id, db.func.lower(Location.name) == name.lower(),
                                same_parent).first()
    if loc is None:
        loc = Location(site_id=site_id, name=name, kind=kind, parent_id=parent.id if parent else None)
        db.session.add(loc)
        db.session.flush()
    return loc


def descendant_ids(location):
    """This location and everything inside it."""
    ids, frontier = {location.id}, [location]
    while frontier:
        node = frontier.pop()
        for child in node.children:
            if child.id not in ids:
                ids.add(child.id)
                frontier.append(child)
    return ids


app.jinja_env.globals.update(type_label=type_label, support_status=support_status, monitor_status=monitor_status)
