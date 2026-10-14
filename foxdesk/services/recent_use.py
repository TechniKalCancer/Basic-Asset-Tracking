"""Which devices a person signed in to most recently, from Google's
per-Chromebook sign-in list (Asset.google_recent_users, newest first,
refreshed by the Google device sync).

Google gives each Chromebook's last time online, and the order of the
people who signed in to it — not when each of them did. So for a device
where the person is the most recent user, the time is when that device
was last online (when they last used it); where someone else signed in
after them, there's no time, only "earlier", and those come after the
timed ones.
"""
from foxdesk.core import db
from foxdesk.models import Asset, AssetRegistry, LoanerCheckout, Person, PersonIdentity

NOT_ASSIGNABLE_STATUSES = ('lost', 'retired', 'repair')


def _emails_by_person(people):
    emails = {}
    ids = [p.id for p in people]
    for p in people:
        if p.email:
            emails[p.email.strip().lower()] = p.id
    if ids:
        for ident in PersonIdentity.query.filter(PersonIdentity.person_id.in_(ids), PersonIdentity.email.isnot(None)):
            emails.setdefault(ident.email.strip().lower(), ident.person_id)
    return emails


def recent_devices(people):
    """{person id: [dict(tag, model, email, when, latest, signins_since,
    holder_id, holder_name, theirs, assignable)]}, most recent first.

    when is the device's last time online if this person was its most
    recent user, else None; signins_since is how many other sign-ins came
    after theirs. assignable means it could be given to them straight from
    the sign-in (they're the latest user, nobody has it, and it isn't a
    loaner, on loan, or lost/retired/in repair) — the same devices Assign
    from Google proposes."""
    emails = _emails_by_person(people)
    out = {p.id: [] for p in people}
    if not emails:
        return out
    rows = (db.session.query(Asset.asset_tag, Asset.google_recent_users, Asset.google_recent_user, Asset.google_last_activity,
                             Asset.google_model, Asset.assigned_to_id, Asset.status, AssetRegistry.id, AssetRegistry.is_loaner)
            .outerjoin(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag)
            .filter(db.or_(Asset.google_recent_users.isnot(None), Asset.google_recent_user.isnot(None))))
    found = []
    for tag, recent, single, last_activity, model, holder_id, status, registry_id, is_loaner in rows:
        users = [u.strip().lower() for u in (recent or ([single] if single else [])) if u]
        seen = set()
        for i, email in enumerate(users):
            pid = emails.get(email)
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            d = dict(tag=tag, model=model, email=email, when=last_activity if i == 0 else None, latest=i == 0,
                     signins_since=i, holder_id=holder_id, holder_name=None, theirs=holder_id == pid,
                     device_last_online=last_activity,
                     assignable=bool(i == 0 and holder_id is None and registry_id and not is_loaner
                                     and status not in NOT_ASSIGNABLE_STATUSES))
            out[pid].append(d)
            found.append(d)
    if found:
        held_by = {d['holder_id'] for d in found if d['holder_id'] and not d['theirs']}
        names = dict(db.session.query(Person.id, Person.first_name + ' ' + Person.last_name).filter(Person.id.in_(held_by))) if held_by else {}
        tags = {d['tag'] for d in found if d['assignable']}
        on_loan = {t for (t,) in db.session.query(LoanerCheckout.asset_tag).filter(
            LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None))} if tags else set()
        for d in found:
            d['holder_name'] = names.get(d['holder_id'])
            d['assignable'] = d['assignable'] and d['tag'] not in on_loan
    for devices in out.values():
        # Timed (they were the latest user) first, newest first; then the rest by how recently the device was online.
        devices.sort(key=lambda d: (not d['latest'], -(d['device_last_online'].timestamp() if d['device_last_online'] else 0)))
    return out
