"""Device agreements: who has to sign what, who has, and recording it.

An active Agreement applies to every active student (at its school, if it
has one). It's complete for a student once each signature it needs is in —
the student's, a parent's, or both — or staff recorded a signed paper copy.
Families sign in the portal: the student signs their own, a parent signs
for each of their students. When a protection plan is offered, whoever
signs for the family (the parent when one has to sign, else the student)
chooses to enroll or not; an enrolled plan with a price is paid online or
marked paid at the office, and paying sets the student's protection plan.
"""
import hashlib
from collections import defaultdict
from decimal import Decimal

from flask import g, has_request_context

from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.models import AGREEMENT_SIGNER_ROLES, Agreement, AgreementSignature, Person
from foxdesk.services.auth import _log_activity
from foxdesk.services.features import feature_enabled

REMIND_EVERY_HOURS = 20


class AgreementError(ValueError):
    """Shown to whoever is signing or recording."""


def body_hash(agreement):
    return hashlib.sha256((agreement.body or '').encode('utf-8')).hexdigest()


def _active():
    if has_request_context():
        cached = getattr(g, '_agreements', None)
        if cached is None:
            cached = g._agreements = Agreement.query.filter_by(is_active=True).order_by(Agreement.id).all()
        return cached
    return Agreement.query.filter_by(is_active=True).order_by(Agreement.id).all()


def applies_to(agreement, person):
    return bool(person and person.role == 'student' and person.is_active
                and (agreement.site_id is None or agreement.site_id == person.site_id))


def agreements_for(person):
    if not feature_enabled('agreements'):
        return []
    return [a for a in _active() if applies_to(a, person)]


def plan_role(agreement):
    """Who makes the protection plan choice for the family."""
    return 'guardian' if agreement.require_guardian else 'student'


def needed_roles(agreement):
    return [r for r, needed in (('student', agreement.require_student), ('guardian', agreement.require_guardian)) if needed]


def status(agreement, signatures):
    """Where one student stands, from their signatures on this agreement:
    dict(complete, missing=[roles], signatures={role: sig}, plan=sig or None,
    plan_unpaid)."""
    by_role = {s.signer_role: s for s in signatures}
    missing = [] if 'paper' in by_role else [r for r in needed_roles(agreement) if r not in by_role]
    plan = next((s for s in by_role.values() if s.plan_choice), None)
    unpaid = bool(plan and plan.plan_choice == 'enrolled' and plan.plan_amount and plan.plan_amount > 0
                  and not plan.plan_paid_at and not plan.plan_waived_at)
    return dict(complete=not missing, missing=missing, signatures=by_role, plan=plan, plan_unpaid=unpaid)


def status_for(agreement, person):
    return status(agreement, AgreementSignature.query.filter_by(agreement_id=agreement.id, person_id=person.id).all())


def agreements_complete(person):
    """True when every agreement that applies to this person is fully
    signed (and for anyone they don't apply to, like staff)."""
    return all(status_for(a, person)['complete'] for a in agreements_for(person)) if person else True


def role_for_viewer(viewer, agreement, person):
    """The signature a portal visitor can give for this student, or None."""
    me = viewer.get('person')
    if me is not None and me.id == person.id and agreement.require_student:
        return 'student'
    if agreement.require_guardian and any(c.id == person.id for c in viewer.get('children') or []):
        return 'guardian'
    return None


def pending_for_viewer(viewer):
    """[(agreement, student, role)] the portal visitor still has to sign."""
    out = []
    people = ([viewer['person']] if viewer.get('person') else []) + list(viewer.get('children') or [])
    for person in people:
        for agreement in agreements_for(person):
            role = role_for_viewer(viewer, agreement, person)
            if role and role in status_for(agreement, person)['missing']:
                out.append((agreement, person, role))
    return out


def sign(agreement, person, role, signer_name, signer_email=None, plan_choice=None, ip_address=None,
         user_agent=None, recorded_by=None):
    """Record a signature. Does not commit; returns it."""
    if role not in AGREEMENT_SIGNER_ROLES:
        raise AgreementError('Unknown signer.')
    if not agreement.is_active or not applies_to(agreement, person):
        raise AgreementError('That agreement isn\'t open for signing for this student.')
    signer_name = (signer_name or '').strip()
    if len(signer_name) < 3:
        raise AgreementError('Type your full name to sign.')
    if AgreementSignature.query.filter_by(agreement_id=agreement.id, person_id=person.id, signer_role=role).first():
        raise AgreementError('This was already signed.')
    chooses_plan = agreement.plan_offered and role in (plan_role(agreement), 'paper')
    if chooses_plan and plan_choice not in ('enrolled', 'declined'):
        raise AgreementError(f'Choose whether to enroll in {agreement.plan_name or "the protection plan"}.')
    price = Decimal(agreement.plan_price or 0)
    sig = AgreementSignature(
        agreement_id=agreement.id, person_id=person.id, signer_role=role, signer_name=signer_name[:160],
        signer_email=(signer_email or '')[:255] or None, ip_address=(ip_address or '')[:64] or None,
        user_agent=(user_agent or '')[:255] or None, body_sha256=body_hash(agreement), recorded_by=recorded_by,
        plan_choice=plan_choice if chooses_plan else None,
        plan_amount=price if chooses_plan and plan_choice == 'enrolled' else None)
    db.session.add(sig)
    if sig.plan_choice == 'enrolled' and price <= 0:
        sig.plan_paid_at = utcnow()
        person.insurance_opted_in = True
    how = f' (recorded by {recorded_by})' if recorded_by else ''
    plan = f'; plan {sig.plan_choice}' if sig.plan_choice else ''
    _log_activity('agreement_signed', f'{AGREEMENT_SIGNER_ROLES[role]} signature on "{agreement.title}" for '
                                      f'{person.full_name}: {signer_name}{how}{plan}.', site_id=person.site_id)
    return sig


def mark_plan_paid(sig, how):
    """The family paid for the plan. Does not commit."""
    if sig.plan_paid_at:
        return
    sig.plan_paid_at = utcnow()
    sig.person.insurance_opted_in = True
    _log_activity('fee_paid', f'{sig.agreement.plan_name or "Protection plan"} for {sig.person.full_name} paid '
                              f'{how}: ${sig.plan_amount or 0:.2f}.', site_id=sig.person.site_id)


def students_for(agreement, site_ids=None):
    query = Person.query.filter(Person.role == 'student', Person.is_active.is_(True))
    if agreement.site_id:
        query = query.filter(Person.site_id == agreement.site_id)
    if site_ids is not None:
        query = query.filter(Person.site_id.in_(site_ids or [-1]))
    return query.order_by(Person.last_name, Person.first_name).all()


def progress(agreement, site_ids=None):
    """(rows, counts) for the agreement page: one row per student it applies to."""
    sigs = defaultdict(list)
    for s in agreement.signatures:
        sigs[s.person_id].append(s)
    rows, counts = [], defaultdict(int)
    for person in students_for(agreement, site_ids):
        st = status(agreement, sigs.get(person.id, []))
        rows.append(dict(person=person, **st))
        counts['students'] += 1
        counts['complete' if st['complete'] else ('partial' if st['signatures'] else 'unsigned')] += 1
        if st['plan'] and st['plan'].plan_choice == 'enrolled':
            counts['enrolled'] += 1
            counts['plan_unpaid'] += int(st['plan_unpaid'])
    return rows, counts


def reminder_recipients(agreement, rows):
    """{email: [student first names]} for everyone who still has to sign,
    through a portal that's switched on."""
    out = defaultdict(list)
    for row in rows:
        if row['complete']:
            continue
        p = row['person']
        if 'guardian' in row['missing'] and p.guardian_email and feature_enabled('parent_portal'):
            out[p.guardian_email.strip().lower()].append(p.first_name)
        if 'student' in row['missing'] and p.email and feature_enabled('portal'):
            out[p.email.strip().lower()].append(p.first_name)
    return out


def unsigned_agreements(person):
    """The agreements this person still needs signed (for staff pages)."""
    return [a for a in agreements_for(person) if not status_for(a, person)['complete']]


def agreement_rows(person):
    """[(agreement, status)] for a person's page."""
    return [(a, status_for(a, person)) for a in agreements_for(person)]


def short_name(agreement):
    """The People list column for an agreement: its short name, else 'AUP'
    for an acceptable use policy, 'CB' for a Chromebook agreement, else
    the title's initials."""
    if agreement.short_name:
        return agreement.short_name
    title = (agreement.title or '').lower()
    if 'acceptable use' in title:
        return 'AUP'
    if 'chromebook' in title:
        return 'CB'
    words = [w for w in (agreement.title or '').split() if w[:1].isalnum()]
    return ''.join(w[0].upper() for w in words[:4]) or 'Agreement'


def list_columns():
    """The active agreements shown as checkbox columns on the People list."""
    return _active() if feature_enabled('agreements') else []


def statuses_for(agreements, people):
    """{(agreement id, person id): status} for a page of people, in one query."""
    ids = [p.id for p in people]
    if not agreements or not ids:
        return {}
    sigs = defaultdict(list)
    for s in AgreementSignature.query.filter(AgreementSignature.agreement_id.in_([a.id for a in agreements]),
                                             AgreementSignature.person_id.in_(ids)):
        sigs[(s.agreement_id, s.person_id)].append(s)
    return {(a.id, p.id): status(a, sigs.get((a.id, p.id), [])) for a in agreements for p in people if applies_to(a, p)}


def signed_filter(agreement):
    """SQL condition on Person: this agreement is fully signed for them."""
    def has(role):
        return db.exists().where(AgreementSignature.agreement_id == agreement.id,
                                 AgreementSignature.person_id == Person.id, AgreementSignature.signer_role == role)
    needed = [has(r) for r in needed_roles(agreement)]
    return db.or_(has('paper'), db.and_(*needed)) if needed else has('paper')


def record_paper(agreement, person, actor, signer_name=None, plan_choice=None):
    """Mark an agreement signed on paper for one student. Returns the
    signature, or None if it was already complete. Does not commit."""
    if status_for(agreement, person)['complete']:
        return None
    return sign(agreement, person, 'paper', signer_name or person.guardian_name or person.full_name,
                plan_choice=plan_choice, recorded_by=actor)


def undo_paper(agreement, person, actor):
    """Remove a paper signature recorded by staff (a mistake). Signatures
    given in the portal are never removed this way. Returns True if one was
    removed. Does not commit."""
    sig = AgreementSignature.query.filter_by(agreement_id=agreement.id, person_id=person.id, signer_role='paper').first()
    if sig is None or sig.plan_paid_at:
        return False
    db.session.delete(sig)
    _log_activity('agreement_signed', f'Removed the paper copy of "{agreement.title}" for {person.full_name} ({actor}).',
                  site_id=person.site_id)
    return True


app.jinja_env.globals.update(unsigned_agreements=unsigned_agreements, agreement_rows=agreement_rows,
                             agreement_short_name=short_name)
