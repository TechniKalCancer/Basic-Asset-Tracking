"""Dashboard charts, Data Quality checks, and the Google sign-in mismatch check."""
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from flask import request, url_for
from foxdesk.core import db
from foxdesk.services.features import feature_enabled
from foxdesk.models import (
    PersonIdentity,
    Asset,
    AssetRegistry,
    AssignmentHistory,
    DeviceModel,
    Incident,
    LoanerCheckout,
    Person,
    Repair,
    SigninReview,
    Site,
    Ticket,
    TicketCharge,
)
from foxdesk.services.util import _top_n_with_other
from foxdesk.services.auth import _has_permission
from foxdesk.services.scoping import (
    _filter_registry_by_warranty,
    _scope_people,
    _scope_registry,
    _scope_repairs,
    _scope_tickets,
)


def _dashboard_charts(site_ids):
    """Data for the Dashboard's trend charts, as plain dicts handed to
    static/js/charts.js. Bucketing happens in Python rather than SQL because
    week/date truncation differs between SQLite (local dev) and Postgres,
    and the row counts involved (a year of tickets/incidents) are small."""
    charts = []
    now = datetime.utcnow()
    year_ago = now - timedelta(days=365)

    if _has_permission('tickets'):
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks = [this_monday - timedelta(weeks=i) for i in range(11, -1, -1)]
        opened = _scope_tickets(Ticket.query, site_ids).filter(Ticket.created_at >= weeks[0]) \
            .with_entities(Ticket.created_at).all()
        counts = defaultdict(int)
        for (created_at,) in opened:
            counts[(created_at - timedelta(days=created_at.weekday())).date()] += 1
        charts.append({
            'id': 'tickets_weekly', 'type': 'columns', 'title': 'Tickets opened per week',
            'subtitle': 'Last 12 weeks', 'link': url_for('admin_tickets'),
            'unit': 'ticket', 'series': [{'name': 'Tickets opened'}],
            'rows': [{'label': f'{w.strftime("%b")} {w.day}',
                      'tip': f'Week of {w.strftime("%b %d, %Y")}',
                      'values': [counts.get(w.date(), 0)]} for w in weeks],
        })

    if _has_permission('repairs'):
        repairs = _scope_repairs(Repair.query, site_ids).filter(Repair.sent_at >= year_ago).all()
        tags = {r.asset_tag for r in repairs}
        registry = {r.asset_tag: r for r in AssetRegistry.query.filter(AssetRegistry.asset_tag.in_(tags))} if tags else {}

        def model_label(row):
            if not row:
                return 'Not in registry'
            if row.device_model:
                return row.device_model.full_name
            return row.description or row.device_type.capitalize()
        repair_counts = defaultdict(int)
        for r in repairs:
            repair_counts[model_label(registry.get(r.asset_tag))] += 1
        fleet = defaultdict(int)
        for model, count in _scope_registry(AssetRegistry.query, site_ids).join(DeviceModel) \
                .with_entities(DeviceModel, db.func.count(AssetRegistry.id)).group_by(DeviceModel.id).all():
            fleet[model.full_name] = count
        rows = []
        for label, value in _top_n_with_other(repair_counts):
            tip = f'{value} repair{"s" if value != 1 else ""}'
            if fleet.get(label):
                tip += f' · {fleet[label]} in fleet · {value * 100 / fleet[label]:.1f} per 100 devices'
            rows.append({'label': label, 'tip': tip, 'values': [value]})
        charts.append({
            'id': 'repairs_by_model', 'type': 'hbar', 'title': 'Repairs by device model',
            'subtitle': 'Sent out in the last 12 months', 'link': url_for('admin_repairs'),
            'unit': 'repair', 'series': [{'name': 'Repairs'}], 'rows': rows,
            'empty': 'No repairs logged in the last 12 months.',
        })

    if _has_permission('devices'):
        inc_query = Incident.query.filter(Incident.created_at >= year_ago)
        if site_ids is not None:
            inc_query = inc_query.join(AssetRegistry, AssetRegistry.asset_tag == Incident.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        incidents = inc_query.all()
        damage = defaultdict(int)
        for inc in incidents:
            damage[inc.repair_category.name if inc.repair_category else 'Uncategorized'] += 1
        charts.append({
            'id': 'damage_by_category', 'type': 'hbar', 'title': 'Damage reports by type',
            'subtitle': 'Incidents logged in the last 12 months', 'link': url_for('admin_repair_categories'),
            'unit': 'incident', 'series': [{'name': 'Incidents'}],
            'rows': [{'label': label, 'tip': f'{value} incident{"s" if value != 1 else ""}', 'values': [value]}
                     for label, value in _top_n_with_other(damage)],
            'empty': 'No damage reports in the last 12 months.',
        })

        site_names = {site.id: site.name for site in Site.query.all()}
        tag_sites = {}
        if incidents:
            tag_sites = dict(AssetRegistry.query.filter(
                AssetRegistry.asset_tag.in_({i.asset_tag for i in incidents})
            ).with_entities(AssetRegistry.asset_tag, AssetRegistry.site_id).all())
        fees = defaultdict(lambda: [Decimal('0'), Decimal('0')])  # site -> [paid, unpaid]
        for inc in incidents:
            if inc.fee_charged and inc.fee_amount:
                site = site_names.get(tag_sites.get(inc.asset_tag), 'No site')
                fees[site][0 if inc.paid_at else 1] += inc.fee_amount
        if _has_permission('tickets'):
            ticket_charges = _scope_tickets(TicketCharge.query.join(Ticket, Ticket.id == TicketCharge.ticket_id), site_ids) \
                .filter(TicketCharge.created_at >= year_ago).with_entities(TicketCharge, Ticket.site_id).all()
            for charge, site_id in ticket_charges:
                fees[site_names.get(site_id, 'No site')][0 if charge.paid_at else 1] += charge.amount
        charts.append({
            'id': 'fees_by_site', 'type': 'hbar', 'stacked': True, 'money': True,
            'title': 'Fees billed by site', 'subtitle': 'Damage and ticket charges, last 12 months',
            'link': url_for('admin_fees', status='all'),
            'series': [{'name': 'Paid'}, {'name': 'Unpaid'}],
            'rows': [{'label': site, 'values': [float(paid), float(unpaid)]}
                     for site, (paid, unpaid) in sorted(fees.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))],
            'empty': 'No fees billed in the last 12 months.',
        })
    return charts


DATA_QUALITY_ROW_LIMIT = 250


PRIMARY_DEVICE_TYPES = ('chromebook', 'laptop', 'ipad')


STALE_REPAIR_DAYS = 30


def _device_cells(row, asset=None, person=None, extra=None):
    cells = [row.asset_tag, row.serial_number or '—', row.device_type,
             row.site.name if row.site else '—']
    if person is not None or asset is not None:
        holder = person or (asset.assigned_to if asset else None)
        cells.append(holder.full_name if holder else '—')
    return cells + list(extra or [])


def _data_quality_checks(site_ids):
    """Returns a list of check dicts: key, title, why, severity
    ('error' = almost certainly wrong data, 'warning' = needs a human look,
    'info' = worth knowing), columns, rows [{'cells': [...], 'link': url}],
    and count. rows is the full list (the CSV export needs all of them) —
    the page itself only renders the first DATA_QUALITY_ROW_LIMIT."""
    checks = []

    def add(key, title, why, severity, columns, query_rows, row_fn):
        rows = [row_fn(r) for r in query_rows]
        checks.append({'key': key, 'title': title, 'why': why, 'severity': severity,
                       'columns': columns, 'rows': rows, 'count': len(rows)})

    device_cols = ['Asset Tag', 'Serial', 'Type', 'Site']
    registry = _scope_registry(AssetRegistry.query, site_ids)
    registry_asset = registry.join(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
    dev_link = lambda tag: url_for('admin_asset_assign', asset_tag=tag)

    # Serials are unique as stored, but "5CD1234ABC" vs "5cd1234abc " vs
    # "5CD-1234ABC" are the same machine typed three ways.
    norm = db.func.upper(db.func.replace(db.func.replace(db.func.trim(AssetRegistry.serial_number), '-', ''), ' ', ''))
    dupe_keys = [k for (k,) in registry.filter(AssetRegistry.serial_number.isnot(None), AssetRegistry.serial_number != '')
                 .with_entities(norm).group_by(norm).having(db.func.count(AssetRegistry.id) > 1).all()]
    dupes = registry.filter(norm.in_(dupe_keys)).order_by(norm, AssetRegistry.asset_tag).all() if dupe_keys else []
    add('duplicate_serials', 'Duplicate serial numbers',
        'Same serial (ignoring case, spaces, and dashes) on more than one asset tag — usually a device entered twice.',
        'error', device_cols, dupes, lambda r: {'cells': _device_cells(r), 'link': dev_link(r.asset_tag)})

    missing_serial = registry.filter(db.or_(AssetRegistry.serial_number.is_(None), AssetRegistry.serial_number == '')) \
        .filter(AssetRegistry.device_type != 'charger').order_by(AssetRegistry.asset_tag).all()
    add('missing_serial', 'Devices with no serial number',
        'Can\'t be matched to Google/vendor records or warranty claims without one. Chargers are excluded.',
        'warning', device_cols, missing_serial, lambda r: {'cells': _device_cells(r), 'link': dev_link(r.asset_tag)})

    inactive_holders = registry_asset.join(Person, Person.id == Asset.assigned_to_id) \
        .filter(Person.is_active.is_(False)).with_entities(AssetRegistry, Asset, Person) \
        .order_by(Person.last_name, Person.first_name).all()
    add('inactive_holders', 'Devices still assigned to inactive people',
        'Graduated or withdrawn, but the record says they still have a device — collect it or mark it lost.',
        'error', device_cols + ['Assigned To', 'Grad Year'], inactive_holders,
        lambda t: {'cells': _device_cells(t[0], person=t[2], extra=[t[2].grad_year or '—']), 'link': dev_link(t[0].asset_tag)})

    status_mismatch = registry_asset.filter(db.or_(
        db.and_(Asset.status == 'assigned', Asset.assigned_to_id.is_(None)),
        db.and_(Asset.assigned_to_id.isnot(None), Asset.status.in_(['available', 'retired', 'lost'])),
    )).with_entities(AssetRegistry, Asset).order_by(AssetRegistry.asset_tag).all()
    add('status_mismatch', 'Status doesn\'t match assignment',
        'Marked "assigned" with nobody assigned, or assigned to someone while marked available/retired/lost.',
        'error', device_cols + ['Assigned To', 'Status'], status_mismatch,
        lambda t: {'cells': _device_cells(t[0], asset=t[1], extra=[t[1].status]), 'link': dev_link(t[0].asset_tag)})

    no_site = registry.filter(AssetRegistry.site_id.is_(None)).order_by(AssetRegistry.asset_tag).all() if site_ids is None else []
    if site_ids is None:
        add('no_site', 'Devices with no site',
            'Invisible to site-scoped staff until a site is set (Devices → Set Device Sites).',
            'warning', device_cols, no_site, lambda r: {'cells': _device_cells(r), 'link': dev_link(r.asset_tag)})

    no_model = registry.filter(AssetRegistry.device_model_id.is_(None),
                               db.or_(AssetRegistry.description.is_(None), AssetRegistry.description == ''),
                               AssetRegistry.device_type.in_(PRIMARY_DEVICE_TYPES)) \
        .order_by(AssetRegistry.asset_tag).all()
    add('no_model', 'Devices with no model or description',
        'Repair-by-model charts and refresh planning can\'t count these.',
        'info', device_cols, no_model, lambda r: {'cells': _device_cells(r), 'link': dev_link(r.asset_tag)})

    held_primary = db.session.query(Asset.assigned_to_id, db.func.count(Asset.id)) \
        .join(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag) \
        .filter(Asset.assigned_to_id.isnot(None), AssetRegistry.device_type.in_(PRIMARY_DEVICE_TYPES)) \
        .filter(AssetRegistry.is_loaner.is_(False))
    if site_ids is not None:
        held_primary = held_primary.filter(AssetRegistry.site_id.in_(site_ids))
    multi = dict(held_primary.group_by(Asset.assigned_to_id).having(db.func.count(Asset.id) > 1).all())
    multi_people = Person.query.filter(Person.id.in_(list(multi))).order_by(Person.last_name).all() if multi else []
    add('multiple_devices', 'People with more than one primary device',
        'More than one Chromebook/laptop/iPad (loaners excluded) — often an old device never checked back in.',
        'warning', ['Name', 'Email', 'Role', 'Devices'], multi_people,
        lambda p: {'cells': [p.full_name, p.email, p.role, multi[p.id]], 'link': url_for('admin_person_history', person_id=p.id)})

    with_device = db.session.query(Asset.assigned_to_id) \
        .join(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag) \
        .filter(Asset.assigned_to_id.isnot(None), AssetRegistry.device_type.in_(PRIMARY_DEVICE_TYPES))
    no_device = _scope_people(Person.query, site_ids).filter(
        Person.role == 'student', Person.is_active.is_(True), ~Person.id.in_(with_device),
    ).order_by(Person.last_name, Person.first_name).all()
    add('students_without_device', 'Active students with no device',
        '1:1 gap — every active student should have a Chromebook/laptop/iPad assigned.',
        'info', ['Name', 'Email', 'Site', 'Grad Year'], no_device,
        lambda p: {'cells': [p.full_name, p.email, p.site.name if p.site else '—', p.grad_year or '—'],
                   'link': url_for('admin_person_edit', person_id=p.id)})

    students_no_guardian = _scope_people(Person.query, site_ids).filter(
        Person.role == 'student', Person.is_active.is_(True),
        db.or_(Person.guardian_email.is_(None), Person.guardian_email == ''),
    ).order_by(Person.last_name, Person.first_name).all()
    add('students_no_guardian', 'Active students with no parent/guardian email',
        'Damage notices can\'t be sent for these. Add guardian_email to your People CSV import from the SIS.',
        'info', ['Name', 'Email', 'Site'], students_no_guardian,
        lambda p: {'cells': [p.full_name, p.email, p.site.name if p.site else '—'],
                   'link': url_for('admin_person_edit', person_id=p.id)})

    stale_cutoff = datetime.utcnow() - timedelta(days=STALE_REPAIR_DAYS)
    stale_repairs = _scope_repairs(Repair.query, site_ids).filter(
        Repair.returned_at.is_(None), Repair.sent_at < stale_cutoff).order_by(Repair.sent_at).all()
    add('stale_repairs', f'Repairs open more than {STALE_REPAIR_DAYS} days',
        'Chase the vendor, or close it out if the device came back and nobody marked it returned.',
        'warning', ['Asset Tag', 'Sent', 'Days Out', 'Issue'], stale_repairs,
        lambda r: {'cells': [r.asset_tag, r.sent_at.strftime('%Y-%m-%d'), (datetime.utcnow() - r.sent_at).days,
                             (r.issue_description or '—')[:80]],
                   'link': url_for('admin_repair_detail', repair_id=r.id)})

    expired_in_use = _filter_registry_by_warranty(registry_asset, 'expired') \
        .filter(Asset.assigned_to_id.isnot(None)).with_entities(AssetRegistry, Asset) \
        .order_by(AssetRegistry.warranty_expiration).all()
    add('expired_in_use', 'Out-of-warranty devices still in use',
        'Assigned devices whose warranty has expired — repairs on these come out of the budget. Useful for refresh planning.',
        'info', device_cols + ['Assigned To', 'Warranty Ended'], expired_in_use,
        lambda t: {'cells': _device_cells(t[0], asset=t[1], extra=[t[0].warranty_expiration.isoformat()]),
                   'link': dev_link(t[0].asset_tag)})

    if feature_enabled('google'):
        never_synced = registry_asset.filter(AssetRegistry.device_type == 'chromebook',
                                             Asset.google_last_sync_at.is_(None),
                                             Asset.status.notin_(['retired', 'lost'])) \
            .with_entities(AssetRegistry).order_by(AssetRegistry.asset_tag).all()
        add('never_synced', 'Chromebooks never matched in Google',
            'No Google Admin record found by serial — typo\'d serial, never enrolled, or deprovisioned.',
            'warning', device_cols, never_synced, lambda r: {'cells': _device_cells(r), 'link': dev_link(r.asset_tag)})

    severity_order = {'error': 0, 'warning': 1, 'info': 2}
    checks.sort(key=lambda c: (c['count'] == 0, severity_order[c['severity']]))
    return checks


SIGNIN_WINDOW_DAYS_CHOICES = (7, 30, 90)


SIGNIN_DEFAULT_WINDOW_DAYS = 30


SIGNIN_HANDOFF_GRACE_DAYS = 3  # a new holder often doesn't sign in for a day or two after pickup


SIGNIN_CATEGORIES = OrderedDict([
    # key: (label, severity, explanation)
    ('wrong_student',     ('Another student\'s device', 'high',
                           'A student who isn\'t the assigned holder is the most recent sign-in.')),
    ('swapped',           ('Swapped devices', 'high',
                           'Two students are each signing in to the other\'s assigned device.')),
    ('inactive_person',   ('Withdrawn/graduated account', 'high',
                           'The most recent sign-in belongs to someone marked inactive.')),
    ('lost_in_use',       ('Lost/retired device in use', 'high',
                           'Marked lost or retired, but someone has been signing in to it.')),
    ('unassigned_in_use', ('Unassigned device in use', 'medium',
                           'Nobody is assigned (or no loaner is checked out), but someone signed in after it was returned.')),
    ('same_name',         ('Same name, different account', 'medium',
                           'Signed in with a different People record that has the holder\'s exact name — one student with two accounts, or two students who share a name.')),
    ('unknown_account',   ('Account not in People', 'medium',
                           'Signed in with an account that doesn\'t match anyone in People.')),
    ('previous_holder',   ('Previous holder still signing in', 'low',
                           'The last sign-in is someone who used to have this device.')),
    ('staff_signin',      ('Staff sign-in', 'low',
                           'A staff account — usually a tech or teacher helping out.')),
])


SIGNIN_SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


def _signin_mismatches(site_ids, window_days=SIGNIN_DEFAULT_WINDOW_DAYS, include_reviewed=False, only_tags=None):
    """Returns a list of mismatch dicts (asset_tag, signin_email, signer,
    expected, category, severity, last_activity, note, reviewed), most
    severe first. Only devices that were actually online within
    window_days are considered — recentUsers is a history, and a device
    sitting in a closet still lists whoever used it last spring.
    only_tags narrows to specific devices (the device page's own flag) —
    swap detection then can't see the other half of a swap, which is fine
    for a single-device badge."""
    now = datetime.utcnow()
    cutoff = now - timedelta(days=window_days)

    rows = _scope_registry(AssetRegistry.query, site_ids) \
        .join(Asset, Asset.asset_tag == AssetRegistry.asset_tag) \
        .filter(Asset.google_recent_user.isnot(None), Asset.google_last_activity >= cutoff)
    if only_tags is not None:
        rows = rows.filter(AssetRegistry.asset_tag.in_(only_tags))
    rows = rows.with_entities(AssetRegistry, Asset).all()
    if not rows:
        return []
    tags = [r.asset_tag for r, _ in rows]

    people_by_email = {p.email.lower(): p for p in Person.query.all()}
    # Every linked account/alias email counts as that person — a merged
    # duplicate's old email or a second Google account isn't someone else.
    for ident in PersonIdentity.query.filter(PersonIdentity.person_id.isnot(None),
                                             PersonIdentity.email.isnot(None)):
        people_by_email.setdefault(ident.email, ident.person)
    people_by_id = {p.id: p for p in people_by_email.values()}

    open_assignments = {h.asset_tag: h for h in AssignmentHistory.query.filter(
        AssignmentHistory.asset_tag.in_(tags), AssignmentHistory.unassigned_at.is_(None))}
    open_loaners = {l.asset_tag: l for l in LoanerCheckout.query.filter(
        LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None))}

    # Who used to have each device, and when it was last handed back — a
    # sign-in by the previous holder, or from before the return, isn't news.
    previous_ids = defaultdict(set)
    last_returned = {}
    for h in AssignmentHistory.query.filter(AssignmentHistory.asset_tag.in_(tags),
                                            AssignmentHistory.unassigned_at.isnot(None)):
        if h.person_id:
            previous_ids[h.asset_tag].add(h.person_id)
        last_returned[h.asset_tag] = max(last_returned.get(h.asset_tag, h.unassigned_at), h.unassigned_at)
    for l in LoanerCheckout.query.filter(LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.isnot(None)):
        if l.person_id:
            previous_ids[l.asset_tag].add(l.person_id)
        last_returned[l.asset_tag] = max(last_returned.get(l.asset_tag, l.checked_in_at), l.checked_in_at)

    # Every device each person is *supposed* to have, for the "their own
    # device is X" note and swap detection.
    own_devices = defaultdict(list)
    for a in Asset.query.filter(Asset.assigned_to_id.isnot(None)).with_entities(Asset.asset_tag, Asset.assigned_to_id):
        own_devices[a.assigned_to_id].append(a.asset_tag)
    for l in LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None), LoanerCheckout.person_id.isnot(None)):
        own_devices[l.person_id].append(l.asset_tag)

    reviewed = {(r.asset_tag, r.signin_email): r for r in SigninReview.query.filter(SigninReview.asset_tag.in_(tags))}

    results = []
    for registry_row, asset in rows:
        recent = asset.google_recent_users or [asset.google_recent_user.lower()]
        signin_email = recent[0]
        signer = people_by_email.get(signin_email)
        # A duplicate profile merged into another (custom_fields.merged_into,
        # see maintenance/2026-10-08_merge_duplicate_people.py) still shows
        # up in older Google sign-ins — count those as the surviving record.
        for _ in range(5):
            merged_into = (signer.custom_fields or {}).get('merged_into') if signer else None
            if not merged_into or merged_into not in people_by_id:
                break
            signer = people_by_id[merged_into]

        expected = []
        held_since = None
        loaner = open_loaners.get(registry_row.asset_tag)
        if loaner and loaner.person_id in people_by_id:
            expected.append(people_by_id[loaner.person_id])
            held_since = loaner.checked_out_at
        if asset.assigned_to_id and asset.assigned_to_id in people_by_id:
            if people_by_id[asset.assigned_to_id] not in expected:
                expected.append(people_by_id[asset.assigned_to_id])
            assignment = open_assignments.get(registry_row.asset_tag)
            if assignment and not held_since:
                held_since = assignment.assigned_at
        expected_ids = {p.id for p in expected}

        is_expected = bool(signer and signer.id in expected_ids)
        if is_expected and asset.status not in ('lost', 'retired'):
            continue  # the normal case: the right person

        note = []
        category = None
        if asset.status in ('lost', 'retired'):
            category = 'lost_in_use'
            if is_expected:
                note.append('It\'s the assigned holder signing in — the device may have turned up.')
        elif not expected:
            returned_at = last_returned.get(registry_row.asset_tag)
            # Only flag use AFTER it came back — before that, whoever had it
            # was supposed to be signing in.
            if returned_at and asset.google_last_activity <= returned_at:
                continue
            category = 'unassigned_in_use'
            if registry_row.is_loaner:
                note.append('Loaner in the pool with no checkout recorded.')
        elif not signer:
            category = 'unknown_account'
        elif signer.id in previous_ids[registry_row.asset_tag]:
            # Checked before is_active: a graduate who had this device before
            # it was reassigned is old history, not a withdrawn student
            # still using it.
            category = 'previous_holder'
            if held_since and (now - held_since).days < SIGNIN_HANDOFF_GRACE_DAYS:
                continue  # just handed over — give the new holder a chance to sign in
            if held_since:
                note.append(f'Handed to the current holder {(now - held_since).days} day(s) ago.')
        elif not signer.is_active:
            category = 'inactive_person'
        elif any(p.full_name.strip().lower() == signer.full_name.strip().lower() for p in expected):
            # Most often a student whose Google account changed (a new
            # account got auto-created in People, the device is still on the
            # old record) — a records problem, not a student on someone
            # else's device. Never auto-merged: some really are two kids.
            category = 'same_name'
            note.append(f'Signed in as {signin_email}; the device is assigned to '
                        f'{", ".join(p.email for p in expected)}.')
        elif signer.role == 'staff':
            category = 'staff_signin'
        else:
            category = 'wrong_student'

        if expected and signer and any(p.email.lower() in recent for p in expected):
            note.append('The assigned holder also appears in this device\'s recent sign-ins.')
        if signer:
            others = [t for t in own_devices.get(signer.id, []) if t != registry_row.asset_tag]
            if others:
                note.append(f'{signer.full_name}\'s own device: {", ".join(others)}.')
            elif signer.role == 'student' and signer.is_active:
                note.append(f'{signer.full_name} has no device assigned.')

        review = reviewed.get((registry_row.asset_tag, signin_email))
        if review and not include_reviewed:
            continue
        label, severity, _ = SIGNIN_CATEGORIES[category]
        results.append({
            'asset_tag': registry_row.asset_tag, 'registry_row': registry_row, 'asset': asset,
            'signin_email': signin_email, 'signer': signer, 'expected': expected,
            'category': category, 'label': label, 'severity': severity,
            'last_activity': asset.google_last_activity, 'note': note, 'review': review,
        })

    # Swap detection: A is on B's device and B is on A's device.
    by_pair = {}
    for m in results:
        if m['category'] == 'wrong_student' and m['signer'] and len(m['expected']) == 1:
            by_pair[(m['signer'].id, m['expected'][0].id)] = m
    for (signer_id, holder_id), m in by_pair.items():
        if (holder_id, signer_id) in by_pair:
            m['category'] = 'swapped'
            m['label'], m['severity'], _ = SIGNIN_CATEGORIES['swapped']

    # How many devices that aren't theirs each account is turning up on — a
    # student on three other kids' Chromebooks is a different conversation
    # than one borrowed device.
    foreign_counts = defaultdict(int)
    for m in results:
        if m['severity'] == 'high':
            foreign_counts[m['signin_email']] += 1
    for m in results:
        m['foreign_count'] = foreign_counts.get(m['signin_email'], 0)

    results.sort(key=lambda m: (SIGNIN_SEVERITY_ORDER[m['severity']], -m['foreign_count'],
                                -(m['last_activity'] or datetime.min).timestamp()))
    return results


def _signin_window_days():
    days = request.args.get('days', SIGNIN_DEFAULT_WINDOW_DAYS, type=int)
    return days if days in SIGNIN_WINDOW_DAYS_CHOICES else SIGNIN_DEFAULT_WINDOW_DAYS
