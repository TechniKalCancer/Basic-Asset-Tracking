"""Query helpers that narrow results to the current user's sites."""
from datetime import datetime, timedelta
from foxdesk.core import db
from foxdesk.models import PersonIdentity, ActivityLog, Asset, AssetRegistry, DeviceModel, Person, Repair, Site, Ticket, User


def _scope_registry(query, site_ids):
    """Applies the current session's site scope to an AssetRegistry query. None = unrestricted."""
    if site_ids is None:
        return query
    return query.filter(AssetRegistry.site_id.in_(site_ids))


WARRANTY_WARNING_DAYS = 60


def _filter_registry_by_warranty(query, mode):
    """mode='expiring': warranty runs out within WARRANTY_WARNING_DAYS. mode='expired': already past."""
    today = datetime.utcnow().date()
    if mode == 'expired':
        return query.filter(AssetRegistry.warranty_expiration.isnot(None),
                             AssetRegistry.warranty_expiration < today)
    horizon = today + timedelta(days=WARRANTY_WARNING_DAYS)
    return query.filter(AssetRegistry.warranty_expiration.isnot(None),
                         AssetRegistry.warranty_expiration >= today,
                         AssetRegistry.warranty_expiration <= horizon)


def _filter_registry_by_status(query, status_filter):
    """Filters an AssetRegistry query by live Asset.status. Assets with no Asset
    row yet are implicitly 'available' (the default), so that case is handled
    by excluding tags with any explicit non-available status rather than requiring one."""
    if status_filter == 'available':
        non_available = db.session.query(Asset.asset_tag).filter(Asset.status != 'available')
        return query.filter(~AssetRegistry.asset_tag.in_(non_available))
    matching = db.session.query(Asset.asset_tag).filter(Asset.status == status_filter)
    return query.filter(AssetRegistry.asset_tag.in_(matching))


def _active_device_models():
    return DeviceModel.query.filter_by(is_active=True).order_by(DeviceModel.manufacturer, DeviceModel.model_name).all()


def _person_search_filter(q):
    """
    Multi-token fuzzy match: each whitespace-separated token must match at least
    one field. This lets "John Smith" (or "Smith John") find John Smith even
    though neither single field contains the whole two-word query.
    """
    conditions = []
    for token in q.split():
        like = f'%{token}%'
        conditions.append(db.or_(
            Person.first_name.ilike(like),
            Person.last_name.ilike(like),
            Person.email.ilike(like),
            Person.site.has(Site.name.ilike(like)),
            Person.external_id.ilike(like),
            Person.identities.any(db.or_(PersonIdentity.email.ilike(like), PersonIdentity.username.ilike(like))),
        ))
    return db.and_(*conditions)


def _scope_people(query, site_ids):
    """Applies the current session's site scope to a Person query. None = unrestricted."""
    if site_ids is None:
        return query
    return query.filter(Person.site_id.in_(site_ids))


def _sites_for_actor(site_ids):
    """Sites the current session may pick from. None (super admin) = every site."""
    if site_ids is None:
        return Site.query.order_by(Site.name).all()
    return Site.query.filter(Site.id.in_(site_ids)).order_by(Site.name).all()


def _scope_users(query, site_ids):
    """A site-scoped admin only sees/manages users who share at least one of their sites."""
    if site_ids is None:
        return query
    return query.filter(User.sites.any(Site.id.in_(site_ids)))


def _scope_repairs(query, site_ids):
    """Repair has no site_id of its own — scope via a join through AssetRegistry,
    same pattern _overdue_assignments/_overdue_loaners use."""
    if site_ids is None:
        return query
    return query.join(AssetRegistry, AssetRegistry.asset_tag == Repair.asset_tag) \
        .filter(AssetRegistry.site_id.in_(site_ids))


def _scope_tickets(query, site_ids):
    """Ticket carries its own site_id directly (no join needed, unlike Repair)."""
    if site_ids is None:
        return query
    return query.filter(Ticket.site_id.in_(site_ids))


def _ticket_assignees(site_ids):
    """Users who can be assigned a ticket: active, with can_tickets or is_admin,
    scoped to the current actor's sites same as everything else site-scoped."""
    return _scope_users(User.query, site_ids).filter(
        User.is_active.is_(True), db.or_(User.can_tickets.is_(True), User.is_admin.is_(True)),
    ).order_by(User.username).all()


def _scope_activity_log(query, site_ids):
    """A site-scoped admin only sees rows with a matching site_id; rows with
    no site (Users/Sites CRUD, a multi-site bulk import) are super-admin-only,
    since a None site_id can't be attributed to any one of their sites."""
    if site_ids is None:
        return query
    return query.filter(ActivityLog.site_id.in_(site_ids))
