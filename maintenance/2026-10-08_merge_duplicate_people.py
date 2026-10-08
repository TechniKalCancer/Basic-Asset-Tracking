# Merge duplicate student People records, 2026-10-08. Dry run unless COMMIT=1.
#
# The pattern on production: the July 22 import created each student on their
# OLD Google account (usually no longer in Google, or in /DeleteUsers) and that
# record holds the device; the Google people sync later auto-created a second
# record for the CURRENT account (in a /Students/Class of ... org unit), which is
# what the student actually signs in with. This moves everything onto the
# current-account record and deactivates the old one with
# custom_fields.merged_into, so (a) the people sync still matches the old email
# and doesn't re-create it, and (b) the sign-in check treats old-account
# sign-ins as the surviving record. Run inside the web container:
#   ssh docker-host 'docker exec -i [-e COMMIT=1] -w /app asset-tracking-plus-web-1 python -' < this_file
import os, collections, logging
logging.disable(logging.CRITICAL)
import app as A
from app import app, db

COMMIT = os.environ.get('COMMIT') == '1'
ACTOR = 'Data cleanup (2026-10-08)'
SKIP_NAMES = {'ethan johnson', 'tandra jones'}  # look like genuinely different students — leave for a human
REF_COLUMNS = [(A.Asset, 'assigned_to_id'), (A.AssignmentHistory, 'person_id'), (A.Incident, 'person_id'),
               (A.LoanerCheckout, 'person_id'), (A.Ticket, 'requester_person_id')]
COPY_IF_BLANK = ('grad_year', 'department', 'guardian_name', 'guardian_email', 'site_id')


def log(summary, site_id=None):
    db.session.add(A.ActivityLog(actor_type='system', actor_label=ACTOR, actor_user_id=None, site_id=site_id,
                                 ticket_id=None, action='person_merge', summary=summary[:500]))


def is_current(p):
    return p.is_active and (p.google_org_unit or '').startswith('/Students/')


with app.app_context():
    signs_in = collections.Counter(a.google_recent_user.lower() for a in
                                   A.Asset.query.filter(A.Asset.google_recent_user.isnot(None)))
    groups = collections.defaultdict(list)
    for p in A.Person.query.all():
        groups[p.full_name.strip().lower()].append(p)

    stats = collections.Counter()
    before = {f'{m.__tablename__}.{c}': m.query.filter(getattr(m, c).isnot(None)).count() for m, c in REF_COLUMNS}
    for name, people in sorted(groups.items()):
        if len(people) < 2 or any(p.role != 'student' for p in people):
            continue
        if name in SKIP_NAMES:
            print(f'SKIP {people[0].full_name}: listed as possibly two different students'); stats['skipped'] += 1
            continue
        current = [p for p in people if is_current(p)]
        if len(current) > 1:
            signing = [p for p in current if signs_in.get(p.email.lower())]
            current = signing if len(signing) == 1 else current
        if len(current) != 1:
            print(f'SKIP {people[0].full_name}: no single current account ({[p.email for p in people]})'); stats['skipped'] += 1
            continue
        keep = current[0]
        losers = [p for p in people if p.id != keep.id]
        years = {p.grad_year for p in people if p.grad_year}
        if len(years) > 1:
            print(f'SKIP {keep.full_name}: graduation years disagree {sorted(years)}'); stats['skipped'] += 1
            continue

        for old in losers:
            moved = {}
            for model, col in REF_COLUMNS:
                n = model.query.filter(getattr(model, col) == old.id).update({col: keep.id}, synchronize_session=False)
                if n: moved[f'{model.__tablename__}'] = n
            if old.external_id and not keep.external_id:
                ext, old.external_id = old.external_id, None
                db.session.flush()
                keep.external_id = ext
            for f in COPY_IF_BLANK:
                if getattr(keep, f) in (None, '') and getattr(old, f) not in (None, ''):
                    setattr(keep, f, getattr(old, f))
            keep.insurance_opted_in = keep.insurance_opted_in or old.insurance_opted_in
            old.custom_fields = {**(old.custom_fields or {}), 'merged_into': keep.id}
            old.is_active = False
            print(f'MERGE {old.email} (#{old.id}) -> {keep.email} (#{keep.id})  moved: {moved or "nothing"}')
            log(f'Merged duplicate profile {old.full_name} ({old.email}) into {keep.email} — same student, '
                f'old Google account. Moved: {", ".join(f"{v} {k}" for k, v in moved.items()) or "nothing"}.',
                site_id=keep.site_id)
            stats['merged'] += 1
            stats['devices_moved'] += moved.get('asset', 0)

    db.session.flush()
    after = {f'{m.__tablename__}.{c}': m.query.filter(getattr(m, c).isnot(None)).count() for m, c in REF_COLUMNS}
    assert before == after, f'reference counts changed: {before} -> {after}'
    print('stats:', dict(stats))
    if COMMIT:
        db.session.commit(); print('COMMITTED')
    else:
        db.session.rollback(); print('DRY RUN - rolled back, nothing written')
