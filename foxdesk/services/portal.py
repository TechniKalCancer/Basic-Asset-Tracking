"""Who's signed in to the portal and what they may see.

The portal is for people FoxDesk already knows, never staff admin work:
  - a student or staff member (an active Person, matched by their primary or
    linked email) sees their own devices, tickets and fees;
  - a parent/guardian (the guardian_email on one or more active students)
    sees those students' devices and damage fees.
One email can be both (a teacher who is also a parent).

Sign-in is by Google (when set up) or a one-time link emailed to the
address on file; nobody can create an account. Portal sessions are kept
apart from admin sessions and last an hour without activity.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import session

from foxdesk.core import db
from foxdesk.models import Asset, Incident, LoanerCheckout, Person, PortalLogin, Ticket
from foxdesk.services.features import feature_enabled
from foxdesk.services.identities import find_person, normalize_email

LINK_MINUTES = 20
SESSION_MINUTES = 60
LINKS_PER_EMAIL_PER_HOUR = 3


def children_of(email):
    if not email or not feature_enabled('parent_portal'):
        return []
    return (Person.query.filter(db.func.lower(Person.guardian_email) == email, Person.is_active.is_(True))
            .order_by(Person.first_name).all())


def self_person(email):
    if not email or not feature_enabled('portal'):
        return None
    person = find_person(email=email)
    return person if person and person.is_active else None


def known_email(email):
    email = normalize_email(email)
    return bool(email and (self_person(email) or children_of(email)))


# ─── sessions ─────────────────────────────────────────────────────────────────

def sign_in(email):
    """Start a portal session (dropping any admin session in this browser)."""
    for key in [k for k in session if not k.startswith('_')]:
        session.pop(key, None)
    session['portal_email'] = normalize_email(email)
    session['portal_seen'] = datetime.now(timezone.utc).timestamp()


def sign_out():
    session.pop('portal_email', None)
    session.pop('portal_seen', None)


def current_email():
    """The signed-in portal email, or None (also ends an idle session)."""
    email = session.get('portal_email')
    if not email:
        return None
    now = datetime.now(timezone.utc).timestamp()
    if now - session.get('portal_seen', 0) > SESSION_MINUTES * 60:
        sign_out()
        return None
    session['portal_seen'] = now
    return email


def viewer():
    """dict(email, person, children) for the signed-in visitor, or None."""
    email = current_email()
    if not email:
        return None
    person, children = self_person(email), children_of(email)
    if not person and not children:
        sign_out()
        return None
    return dict(email=email, person=person, children=children)


# ─── one-time sign-in links ───────────────────────────────────────────────────

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def make_link_token(email):
    """A token for an emailed sign-in link, or None when the address isn't
    one FoxDesk knows or has had too many links lately. The caller always
    tells the visitor the same thing either way. Commits."""
    email = normalize_email(email)
    if not known_email(email):
        return None
    recent = PortalLogin.query.filter(PortalLogin.email == email,
                                      PortalLogin.created_at >= datetime.utcnow() - timedelta(hours=1)).count()
    if recent >= LINKS_PER_EMAIL_PER_HOUR:
        return None
    token = secrets.token_urlsafe(32)
    db.session.add(PortalLogin(token_hash=_hash(token), email=email))
    db.session.commit()
    return token


def redeem_link_token(token):
    """The email a fresh, unused link was for (and marks it used), or None. Commits."""
    row = PortalLogin.query.filter_by(token_hash=_hash(token or '')).first()
    if not row or row.used_at or datetime.utcnow() - row.created_at > timedelta(minutes=LINK_MINUTES):
        return None
    row.used_at = datetime.utcnow()
    db.session.commit()
    return row.email if known_email(row.email) else None


# ─── what they can see ────────────────────────────────────────────────────────

def devices_for(person):
    """[(asset_tag, label, kind, due)] — assigned devices and loaners out."""
    out = []
    for a in Asset.query.filter_by(assigned_to_id=person.id).order_by(Asset.asset_tag):
        model = a.google_model or ''
        out.append((a.asset_tag, model, 'assigned', None))
    for l in LoanerCheckout.query.filter_by(person_id=person.id, checked_in_at=None):
        out.append((l.asset_tag, 'Loaner', 'loaner', l.due_date))
    return out


def tickets_for(person, email):
    return (Ticket.query.filter(db.or_(Ticket.requester_person_id == person.id,
                                       db.func.lower(Ticket.requester_email) == email))
            .order_by(Ticket.updated_at.desc()).limit(25).all())


def fees_for(person):
    return (Incident.query.filter(Incident.person_id == person.id, Incident.fee_charged.is_(True))
            .order_by(Incident.created_at.desc()).all())


def damage_for(person):
    return Incident.query.filter_by(person_id=person.id).order_by(Incident.created_at.desc()).limit(20).all()


def may_see_ticket(v, ticket):
    person = v['person']
    return bool(person and (ticket.requester_person_id == person.id
                            or (ticket.requester_email or '').lower() == v['email']))


def may_pay(v, incident):
    ids = {c.id for c in v['children']} | ({v['person'].id} if v['person'] else set())
    return incident.person_id in ids
