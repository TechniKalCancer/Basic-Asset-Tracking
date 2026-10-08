# Production data cleanup, 2026-10-07. Dry run unless COMMIT=1 is set.
# 1) Merge case-duplicate serial pairs (original import row + Google auto-created row).
# 2) Fix the one status/assignment contradiction.
# 3) Give site-less devices a site from their holder or FCHS/FCMS hostname.
# 4) Fill blank descriptions from the cached Google model name.
# Writes only through this one session; nothing here calls a helper that commits on its own.
import os, collections, logging
from datetime import datetime
logging.disable(logging.CRITICAL)
import app as A
from app import app, db

COMMIT = os.environ.get('COMMIT') == '1'
NOW = datetime.utcnow()
ACTOR = 'Data cleanup (2026-10-08)'
HISTORY_MODELS = [A.AssignmentHistory, A.Event, A.Incident, A.Repair, A.LoanerCheckout, A.Ticket,
                  A.AuditScan, A.PendingDeviceAction]

def log(action, summary, site_id=None):
    db.session.add(A.ActivityLog(actor_type='system', actor_label=ACTOR, actor_user_id=None,
                                 site_id=site_id, ticket_id=None, action=action, summary=summary[:500]))

def norm(s): return (s or '').strip().replace('-', '').replace(' ', '').upper()

with app.app_context():
    before = {m.__tablename__: m.query.count() for m in HISTORY_MODELS}
    before['asset_registry'] = A.AssetRegistry.query.count()

    groups = collections.defaultdict(list)
    for r in A.AssetRegistry.query.filter(A.AssetRegistry.serial_number.isnot(None)).all():
        if r.serial_number.strip():
            groups[norm(r.serial_number)].append(r)
    pairs = [v for v in groups.values() if len(v) == 2]
    assert len(pairs) == len([v for v in groups.values() if len(v) > 1]), 'unexpected 3+ duplicate group'

    stats = collections.Counter()
    for rows in pairs:
        info = []
        for r in rows:
            a = A.Asset.query.filter_by(asset_tag=r.asset_tag).first()
            refs = sum(m.query.filter_by(asset_tag=r.asset_tag).count() for m in HISTORY_MODELS)
            open_loaner = A.LoanerCheckout.query.filter_by(asset_tag=r.asset_tag, checked_in_at=None).first()
            is_lower = r.serial_number != r.serial_number.upper()
            info.append(dict(r=r, a=a, refs=refs, held=bool((a and a.assigned_to_id) or open_loaner), lower=is_lower))
        # keeper: the one someone holds, then more history, then the original import row (lowercase serial)
        info.sort(key=lambda i: (i['held'], i['refs'], i['lower']), reverse=True)
        keep, lose = info[0], info[1]
        kr, lr, ka, la = keep['r'], lose['r'], keep['a'], lose['a']
        upper_row = kr if not keep['lower'] else lr
        lower_row = lr if upper_row is kr else kr
        final_serial = upper_row.serial_number.strip()
        stats['keep_' + ('original' if keep['lower'] else 'google')] += 1

        # both held by the same person (one pair): close the duplicate's open assignment instead of keeping two open
        if la and la.assigned_to_id and ka and ka.assigned_to_id:
            assert la.assigned_to_id == ka.assigned_to_id, f'{kr.asset_tag}/{lr.asset_tag} held by different people'
            for h in A.AssignmentHistory.query.filter_by(asset_tag=lr.asset_tag, unassigned_at=None):
                h.unassigned_at = NOW
                h.condition_in = f'Duplicate record merged into {kr.asset_tag}'
            stats['closed_duplicate_assignment'] += 1
        elif la and la.assigned_to_id and not (ka and ka.assigned_to_id):
            raise RuntimeError(f'keeper {kr.asset_tag} unassigned but loser {lr.asset_tag} assigned')

        # move every history row onto the keeper's tag
        moved = 0
        for m in HISTORY_MODELS:
            moved += m.query.filter_by(asset_tag=lr.asset_tag).update({'asset_tag': kr.asset_tag}, synchronize_session=False)
        for rev in A.SigninReview.query.filter_by(asset_tag=lr.asset_tag).all():
            if A.SigninReview.query.filter_by(asset_tag=kr.asset_tag, signin_email=rev.signin_email).first():
                db.session.delete(rev)
            else:
                rev.asset_tag = kr.asset_tag
        stats['history_rows_moved'] += moved

        # registry fields: fill the keeper's blanks from the duplicate
        for f in ('device_model_id', 'site_id', 'purchase_date', 'purchase_cost', 'warranty_expiration', 'loaner_label'):
            if getattr(kr, f) in (None, '') and getattr(lr, f) not in (None, ''):
                setattr(kr, f, getattr(lr, f)); stats['filled_' + f] += 1
        kr.is_loaner = kr.is_loaner or lr.is_loaner
        if lr.custom_fields:
            kr.custom_fields = {**lr.custom_fields, **(kr.custom_fields or {})}
        if kr.device_type == 'other' and lr.device_type != 'other':
            kr.device_type = lr.device_type
        model_desc, note_desc = (upper_row.description or '').strip(), (lower_row.description or '').strip()
        if model_desc and note_desc and model_desc != note_desc:
            kr.description = f'{model_desc} (was: {note_desc})'[:255]
        else:
            kr.description = model_desc or note_desc or None

        # Google cache: take it from whichever copy the sync actually matched
        if ka is None:
            ka = A.Asset(asset_tag=kr.asset_tag, is_valid=True, status='available'); db.session.add(ka)
        if la and la.google_last_sync_at and (not ka.google_last_sync_at or la.google_last_sync_at > ka.google_last_sync_at):
            for f in ('google_model', 'google_org_unit', 'google_recent_user', 'google_recent_users',
                      'google_last_activity', 'google_last_sync_at', 'google_enabled'):
                setattr(ka, f, getattr(la, f))
            stats['google_data_copied'] += 1
        if la and not ka.assigned_to_id and ka.status == 'available' and la.status in ('repair', 'lost', 'retired'):
            ka.status = la.status; stats['status_from_duplicate'] += 1

        lost_tag = lr.asset_tag
        if la: db.session.delete(la)
        db.session.delete(lr)
        db.session.flush()  # free the unique serial before giving it to the keeper
        kr.serial_number = final_serial
        log('device_merge', f'Merged duplicate record {lost_tag} into {kr.asset_tag} — same serial {final_serial} '
                            f'entered twice (different capitalization); {moved} history row(s) moved.', site_id=kr.site_id)
        stats['merged'] += 1

    # 2) status contradicts assignment
    for a in A.Asset.query.filter(A.Asset.assigned_to_id.isnot(None), A.Asset.status == 'available').all():
        a.status = 'assigned'
        log('device_status', f'Status of {a.asset_tag} set to assigned — it is assigned to {a.assigned_to.full_name}.')
        stats['status_fixed'] += 1
    for a in A.Asset.query.filter(A.Asset.assigned_to_id.is_(None), A.Asset.status == 'assigned').all():
        a.status = 'available'
        log('device_status', f'Status of {a.asset_tag} set to available — nobody is assigned to it.')
        stats['status_fixed'] += 1

    # 3) site-less devices
    sites = {s.name: s.id for s in A.Site.query.all()}
    prefix_site = {'FCHS': sites.get('Fox Creek High School'), 'FCMS': sites.get('Fox Creek Middle School')}
    for r in A.AssetRegistry.query.filter(A.AssetRegistry.site_id.is_(None)).all():
        a = A.Asset.query.filter_by(asset_tag=r.asset_tag).first()
        site_id, why = None, None
        if a and a.assigned_to and a.assigned_to.site_id:
            site_id, why = a.assigned_to.site_id, f'its holder {a.assigned_to.full_name}'
        elif r.description and r.description.upper()[:4] in prefix_site:
            site_id, why = prefix_site[r.description.upper()[:4]], f'its hostname {r.description}'
        if site_id:
            r.site_id = site_id
            log('device_edit', f'Set site of {r.asset_tag} from {why}.', site_id=site_id)
            stats['site_set'] += 1
        else:
            stats['site_left_blank'] += 1
            print('   site left blank:', r.asset_tag, r.description)

    # 4) blank descriptions from Google's model name
    for r in A.AssetRegistry.query.filter(db.or_(A.AssetRegistry.description.is_(None), A.AssetRegistry.description == '')).all():
        a = A.Asset.query.filter_by(asset_tag=r.asset_tag).first()
        if a and a.google_model:
            r.description = a.google_model
            stats['description_from_google'] += 1

    db.session.flush()
    after = {m.__tablename__: m.query.count() for m in HISTORY_MODELS}
    after['asset_registry'] = A.AssetRegistry.query.count()
    print('stats:', dict(stats))
    print('row counts before -> after:', {k: (before[k], after[k]) for k in before if before[k] != after[k] or k == 'asset_registry'})
    assert all(before[m.__tablename__] == after[m.__tablename__] for m in HISTORY_MODELS), 'history rows lost!'
    assert after['asset_registry'] == before['asset_registry'] - stats['merged']
    remaining = collections.Counter(norm(r.serial_number) for r in A.AssetRegistry.query.filter(A.AssetRegistry.serial_number.isnot(None)) if r.serial_number.strip())
    print('duplicate serial groups remaining:', sum(1 for v in remaining.values() if v > 1))
    if COMMIT:
        db.session.commit(); print('COMMITTED')
    else:
        db.session.rollback(); print('DRY RUN — rolled back, nothing written')
