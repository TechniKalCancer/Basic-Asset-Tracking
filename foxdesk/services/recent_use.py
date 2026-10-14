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
from foxdesk.models import Asset, PersonIdentity


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
    """{person id: [dict(tag, model, when, latest, signins_since, theirs)]},
    most recent first. when is the device's last time online if this person
    was its most recent user, else None; signins_since is how many other
    sign-ins came after theirs."""
    emails = _emails_by_person(people)
    out = {p.id: [] for p in people}
    if not emails:
        return out
    rows = (Asset.query.filter(db.or_(Asset.google_recent_users.isnot(None), Asset.google_recent_user.isnot(None)))
            .with_entities(Asset.asset_tag, Asset.google_recent_users, Asset.google_recent_user, Asset.google_last_activity,
                           Asset.google_model, Asset.assigned_to_id))
    for tag, recent, single, last_activity, model, assigned_to in rows:
        users = [u.strip().lower() for u in (recent or ([single] if single else [])) if u]
        seen = set()
        for i, email in enumerate(users):
            pid = emails.get(email)
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            out[pid].append(dict(tag=tag, model=model, when=last_activity if i == 0 else None, latest=i == 0,
                                 signins_since=i, theirs=assigned_to == pid, device_last_online=last_activity))
    for devices in out.values():
        # Timed (they were the latest user) first, newest first; then the rest by how recently the device was online.
        devices.sort(key=lambda d: (not d['latest'], -(d['device_last_online'].timestamp() if d['device_last_online'] else 0)))
    return out
