"""One person, one profile; one device, one registry row.

Every sync (Google, AD, Entra, PowerSchool, ...) goes through here to place
an outside account on an existing person, or an outside device record on an
existing registry device, instead of creating duplicates.

People are matched in this order, and only these count as certain:
  1. the account was linked before (same source + external key);
  2. its email is a person's primary email or a linked account/alias email;
  3. its student/staff ID equals a person's external_id.
An exact-name match is NOT certain — two students can share a name — so the
account is held on Accounts to Review with that person suggested.

A person merged into another (custom_fields.merged_into) resolves to the
surviving record everywhere.
"""
from datetime import datetime

from foxdesk.core import db
from foxdesk.models import AssetRegistry, DeviceRecord, Person, PersonIdentity


def normalize_email(email):
    return (email or '').strip().lower() or None


def resolve_merged(person):
    """Follow custom_fields.merged_into to the surviving record."""
    for _ in range(5):
        target = (person.custom_fields or {}).get('merged_into') if person else None
        if not target:
            break
        nxt = Person.query.get(target)
        if not nxt:
            break
        person = nxt
    return person


def find_person(email=None, student_id=None):
    """The person an email or student/staff ID certainly belongs to, or None."""
    email = normalize_email(email)
    person = None
    if email:
        person = Person.query.filter(db.func.lower(Person.email) == email).first()
        if not person:
            ident = PersonIdentity.query.filter(PersonIdentity.email == email,
                                                PersonIdentity.person_id.isnot(None)).first()
            person = ident.person if ident else None
    if not person and student_id:
        person = Person.query.filter(Person.external_id == str(student_id).strip()).first()
    return resolve_merged(person)


def name_matches(first_name, last_name, role=None):
    """Active people with exactly this name (case/space-insensitive)."""
    full = f'{first_name or ""} {last_name or ""}'.strip().lower()
    if not full:
        return []
    query = Person.query.filter(Person.is_active.is_(True),
                                db.func.lower(db.func.trim(Person.first_name + ' ' + Person.last_name)) == full)
    if role:
        query = query.filter(Person.role == role)
    return [p for p in query.all() if not (p.custom_fields or {}).get('merged_into')]


def person_emails(person):
    """Every email that identifies this person: primary + linked accounts."""
    emails = {normalize_email(person.email)}
    emails.update(normalize_email(i.email) for i in person.identities if i.email)
    emails.discard(None)
    return emails


def place_account(source, external_key, email=None, student_id=None, first_name=None, last_name=None,
                  role=None, **fields):
    """Upsert the account (source, external_key) and link it to the person it
    certainly belongs to. Returns (identity, person, how) where how is
    'linked' (already linked before), 'matched' (newly linked by email or
    ID), 'review' (held — an exact-name candidate is suggested),
    'unmatched' (no candidate at all; the caller decides whether to create a
    person, e.g. Google's auto-create), or 'ignored' (an admin marked it as
    nobody's on Accounts to Review). Does not commit."""
    external_key = str(external_key).strip()
    email = normalize_email(email)
    ident = PersonIdentity.query.filter_by(source=source, external_key=external_key).first()
    if ident is None and email:
        # Accounts seeded before a source's real ID was known were keyed by
        # email (see the identities migration) — adopt that row instead of
        # adding a second one for the same account.
        ident = PersonIdentity.query.filter_by(source=source, external_key=email).first()
        if ident is not None:
            ident.external_key = external_key
    if ident is None:
        ident = PersonIdentity(source=source, external_key=external_key)
        db.session.add(ident)
    ident.email = email or ident.email
    for key, value in fields.items():
        setattr(ident, key, value)
    ident.last_synced_at = datetime.utcnow()

    if ident.review_status == 'ignored':
        return ident, None, 'ignored'  # an admin said this account isn't anyone — don't re-link it
    if ident.person_id:
        survivor = resolve_merged(ident.person)
        if survivor and survivor.id != ident.person_id:
            ident.person_id = survivor.id
        return ident, survivor, 'linked'
    person = find_person(email=email, student_id=student_id)
    if person:
        ident.person_id = person.id
        ident.suggested_person_id = None
        return ident, person, 'matched'
    candidates = name_matches(first_name, last_name, role)
    if candidates:
        ident.suggested_person_id = candidates[0].id if len(candidates) == 1 else None
        return ident, None, 'review'
    return ident, None, 'unmatched'


def link_account(ident, person):
    """Attach a held account to a person (Accounts to Review). Does not commit."""
    ident.person_id = resolve_merged(person).id
    ident.suggested_person_id = None
    ident.review_status = None


def add_alias(person, email):
    """An extra email for a person (no outside system). Returns the identity,
    or raises ValueError if that email already belongs to someone. Does not commit."""
    email = normalize_email(email)
    if not email or '@' not in email:
        raise ValueError('Enter a valid email address.')
    owner = find_person(email=email)
    if owner and owner.id != person.id:
        raise ValueError(f'{email} already belongs to {owner.full_name}.')
    if owner and owner.id == person.id:
        raise ValueError(f'{email} is already one of {person.full_name}\'s emails.')
    ident = PersonIdentity(person_id=person.id, source='alias', external_key=email, email=email,
                           last_synced_at=None)
    db.session.add(ident)
    return ident


# ─── Devices ──────────────────────────────────────────────────────────────────

def normalize_serial(serial):
    return (serial or '').strip().replace('-', '').replace(' ', '').upper() or None


def find_registry_row(serial_number=None, hostname=None):
    """The registry device a serial (case/dash-insensitive) or, failing that,
    a hostname matching an existing record or the description, belongs to."""
    serial = normalize_serial(serial_number)
    if serial:
        norm = db.func.upper(db.func.replace(db.func.replace(db.func.trim(AssetRegistry.serial_number), '-', ''), ' ', ''))
        row = AssetRegistry.query.filter(norm == serial).first()
        if row:
            return row
    host = (hostname or '').strip().lower()
    if host:
        rec = DeviceRecord.query.filter(db.func.lower(DeviceRecord.hostname) == host,
                                        DeviceRecord.registry_id.isnot(None)).first()
        if rec:
            return rec.registry_row
    return None


def place_device_record(source, external_key, serial_number=None, hostname=None, **fields):
    """Upsert the record (source, external_key) and attach it to its registry
    device. Returns (record, registry_row or None). Does not commit."""
    external_key = str(external_key).strip()
    rec = DeviceRecord.query.filter_by(source=source, external_key=external_key).first()
    if rec is None:
        rec = DeviceRecord(source=source, external_key=external_key)
        db.session.add(rec)
    rec.serial_number = serial_number or rec.serial_number
    rec.hostname = hostname or rec.hostname
    for key, value in fields.items():
        setattr(rec, key, value)
    rec.last_synced_at = datetime.utcnow()
    if rec.registry_id is None and rec.review_status != 'ignored':
        row = find_registry_row(serial_number=serial_number, hostname=hostname)
        if row:
            rec.registry_id = row.id
    return rec, rec.registry_row


def unlink_account(ident):
    """Detach a synced account from its person and keep it detached (the
    next sync won't re-link it by email); an alias is simply deleted.
    Does not commit."""
    if ident.source == 'alias':
        db.session.delete(ident)
        return
    ident.person_id = None
    ident.suggested_person_id = None
    ident.review_status = 'ignored'


def accounts_to_review_query():
    return PersonIdentity.query.filter(PersonIdentity.person_id.is_(None),
                                       PersonIdentity.review_status.is_(None))
