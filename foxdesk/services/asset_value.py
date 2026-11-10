"""What devices are worth now, and what repairs cost by model.

Value is straight-line: a device loses its purchase cost evenly over its
useful life (years, set per device type) down to salvage_percent of cost,
counted in whole months since the purchase date. A device with no cost or
no purchase date has no value to report and is counted as missing data.

Repair cost by model adds the parts used on each repair (at the part's unit
cost), the time techs logged (at the labor rate), and what families were
billed on the repair's ticket.
"""
from collections import Counter, OrderedDict, defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import (
    Asset, AssetRegistry, AssetValueSettings, END_USER_TYPES, Part, PartMovement, REPAIR_OUTCOMES, Repair, Site,
    TicketCharge,
)

DEFAULT_LIFE = {'chromebook': 4, 'laptop': 4, 'desktop': 5, 'ipad': 4, 'charger': 2, 'hotspot': 3}
DEFAULT_EQUIPMENT_LIFE = 6
DEFAULT_LIFE_FOR_OTHER = 4
CENT = Decimal('0.01')


def value_settings(create=False):
    row = db.session.get(AssetValueSettings, 1)
    if row is None:
        row = AssetValueSettings(id=1, life_years={}, salvage_percent=0)
        if create:
            db.session.add(row)
    return row


def life_for(device_type, settings=None):
    settings = settings or value_settings()
    years = (settings.life_years or {}).get(device_type)
    if years:
        return int(years)
    return DEFAULT_LIFE.get(device_type, DEFAULT_LIFE_FOR_OTHER if device_type in END_USER_TYPES else DEFAULT_EQUIPMENT_LIFE)


def months_between(start, end):
    return max(0, (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0))


def book_value(cost, purchased, life_years, salvage_percent=0, today=None):
    """Value today, or None without a cost and purchase date."""
    if cost is None or purchased is None or not life_years:
        return None
    cost = Decimal(cost)
    floor = cost * Decimal(salvage_percent or 0) / 100
    used = min(1, Decimal(months_between(purchased, today or date.today())) / (Decimal(life_years) * 12))
    return max(floor, cost - (cost - floor) * used).quantize(CENT, rounding=ROUND_HALF_UP)


def device_value(registry_row, settings=None):
    """dict(cost, value, life, fully) for the device page, or None."""
    settings = settings or value_settings()
    life = life_for(registry_row.device_type, settings)
    value = book_value(registry_row.purchase_cost, registry_row.purchase_date, life, settings.salvage_percent)
    if value is None:
        return None
    return dict(cost=Decimal(registry_row.purchase_cost), value=value, life=life,
                fully=months_between(registry_row.purchase_date, date.today()) >= life * 12)


GROUPS = OrderedDict([('funding', 'Funding source'), ('site', 'School'), ('type', 'Device type'), ('year', 'Year bought')])


def value_report(site_ids=None, group='funding'):
    """Rows of dict(key, count, cost, value, fully) per group, plus totals and
    how many devices have no cost or date yet."""
    settings = value_settings()
    query = AssetRegistry.query
    if site_ids is not None:
        query = query.filter(AssetRegistry.site_id.in_(site_ids or [-1]))
    sites = {s.id: s.name for s in Site.query}
    rows, missing, total = defaultdict(lambda: dict(count=0, cost=Decimal('0'), value=Decimal('0'), fully=0)), 0, 0
    for r in query.with_entities(AssetRegistry.device_type, AssetRegistry.purchase_cost, AssetRegistry.purchase_date,
                                 AssetRegistry.funding_source, AssetRegistry.funding_year, AssetRegistry.site_id):
        total += 1
        life = life_for(r.device_type, settings)
        value = book_value(r.purchase_cost, r.purchase_date, life, settings.salvage_percent)
        if value is None:
            missing += 1
            continue
        key = {'funding': r.funding_source or 'Not set', 'site': sites.get(r.site_id, 'No school'),
               'type': r.device_type or 'other',
               'year': str(r.purchase_date.year)}[group]
        bucket = rows[key]
        bucket['count'] += 1
        bucket['cost'] += Decimal(r.purchase_cost)
        bucket['value'] += value
        if months_between(r.purchase_date, date.today()) >= life * 12:
            bucket['fully'] += 1
    out = [dict(key=k, **v) for k, v in sorted(rows.items(), key=lambda kv: -kv[1]['cost'])]
    return dict(rows=out, total_devices=total, missing=missing,
                cost=sum((r['cost'] for r in out), Decimal('0')), value=sum((r['value'] for r in out), Decimal('0')))


def fill_purchase_info(site_ids, match, values, overwrite=False):
    """Set purchase date/cost/funding on every device matching
    match = dict(model, device_type, site_id). Only blank fields unless
    overwrite. Does not commit. Returns how many devices changed."""
    query = AssetRegistry.query
    if site_ids is not None:
        query = query.filter(AssetRegistry.site_id.in_(site_ids or [-1]))
    if match.get('device_type'):
        query = query.filter(AssetRegistry.device_type == match['device_type'])
    if match.get('site_id'):
        query = query.filter(AssetRegistry.site_id == match['site_id'])
    if match.get('model'):
        tags = {t for (t,) in Asset.query.filter(Asset.google_model == match['model']).with_entities(Asset.asset_tag)}
        query = query.filter(db.or_(AssetRegistry.asset_tag.in_(tags), AssetRegistry.description == match['model']))
    changed = 0
    for row in query:
        touched = False
        for field in ('purchase_date', 'purchase_cost', 'funding_source', 'funding_year'):
            new = values.get(field)
            if new in (None, '') or (getattr(row, field) not in (None, '') and not overwrite):
                continue
            if getattr(row, field) != new:
                setattr(row, field, new)
                touched = True
        changed += touched
    return changed


def model_names():
    """Google model names in the fleet, most common first, for the fill form."""
    return [m for m, _ in Counter(m for (m,) in Asset.query.filter(Asset.google_model.isnot(None))
                                  .with_entities(Asset.google_model)).most_common()]


# ─── repair cost by model ─────────────────────────────────────────────────────

def _model_of(registry_row, asset):
    if registry_row is not None and registry_row.device_model:
        return registry_row.device_model.full_name
    if asset is not None and asset.google_model:
        return asset.google_model
    if registry_row is not None and registry_row.description:
        return registry_row.description
    return 'Unknown model'


def repair_costs(site_ids=None, since=None):
    """Per device model: devices, repairs (since `since`), repairs per 100
    devices, parts and labor cost, billed to families, average days out, and
    outcomes. Most repairs first."""
    settings = value_settings()
    rate = Decimal(settings.labor_rate or 0)
    registry = AssetRegistry.query
    if site_ids is not None:
        registry = registry.filter(AssetRegistry.site_id.in_(site_ids or [-1]))
    registry = {r.asset_tag: r for r in registry}
    assets = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(list(registry)))} if registry else {}
    model_of = {tag: _model_of(row, assets.get(tag)) for tag, row in registry.items()}
    stats = defaultdict(lambda: dict(devices=0, repairs=0, parts=Decimal('0'), minutes=0, billed=Decimal('0'),
                                     days=[], outcomes=Counter()))
    for tag, model in model_of.items():
        stats[model]['devices'] += 1

    repairs = Repair.query.filter(Repair.asset_tag.in_(list(registry))) if registry else Repair.query.filter(db.false())
    if since:
        repairs = repairs.filter(Repair.sent_at >= since)
    repairs = repairs.all()
    by_id = {r.id: r for r in repairs}
    for r in repairs:
        s = stats[model_of[r.asset_tag]]
        s['repairs'] += 1
        s['minutes'] += r.labor_minutes or 0
        if r.returned_at:
            s['days'].append((r.returned_at - r.sent_at).days)
            s['outcomes'][r.outcome] += 1
    if by_id:
        for change, unit_cost, repair_id in (db.session.query(PartMovement.change, Part.unit_cost, PartMovement.repair_id)
                                             .join(Part, Part.id == PartMovement.part_id)
                                             .filter(PartMovement.repair_id.in_(list(by_id)), PartMovement.change < 0)):
            stats[model_of[by_id[repair_id].asset_tag]]['parts'] += Decimal(unit_cost or 0) * -change
        ticket_ids = {r.ticket_id: r for r in repairs if r.ticket_id}
        for amount, ticket_id in (db.session.query(TicketCharge.amount, TicketCharge.ticket_id)
                                  .filter(TicketCharge.ticket_id.in_(list(ticket_ids)), TicketCharge.waived_at.is_(None))):
            stats[model_of[ticket_ids[ticket_id].asset_tag]]['billed'] += Decimal(amount)
    out = []
    for model, s in stats.items():
        if not s['repairs']:
            continue
        labor = (rate * s['minutes'] / 60).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append(dict(model=model, devices=s['devices'], repairs=s['repairs'],
                        per100=round(100 * s['repairs'] / s['devices'], 1) if s['devices'] else None,
                        parts=s['parts'], hours=round(s['minutes'] / 60, 1), labor=labor, cost=s['parts'] + labor,
                        billed=s['billed'], avg_days=round(sum(s['days']) / len(s['days']), 1) if s['days'] else None,
                        outcomes={REPAIR_OUTCOMES.get(k, k): v for k, v in s['outcomes'].items() if k}))
    return sorted(out, key=lambda r: (-r['repairs'], r['model']))


def school_year_start(today=None):
    today = today or utcnow().date()
    return date(today.year if today.month >= 7 else today.year - 1, 7, 1)
