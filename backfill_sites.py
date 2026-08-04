"""
One-time backfill for multi-site support. Safe to re-run (only touches rows
that still have no site_id). Run inside the app container:

    docker compose exec web python backfill_sites.py
"""
from app import app, db, Site, Person, AssetRegistry, Asset, LoanerCheckout

CANONICAL_SITES = ['Fox Creek Middle School', 'Fox Creek High School']
LEGACY_SITE_ALIASES = {
    'fchs': 'Fox Creek High School',  # known typo row found during the Google/roster sync
}
DESCRIPTION_PREFIX_TO_SITE = {
    'FCMS': 'Fox Creek Middle School',
    'FCHS': 'Fox Creek High School',
}


def get_or_create_site(name):
    site = Site.query.filter_by(name=name).first()
    if not site:
        site = Site(name=name)
        db.session.add(site)
        db.session.flush()
    return site


def backfill_people(sites_by_name):
    updated = 0
    unmapped = {}
    for p in Person.query.filter(Person.site_id.is_(None)).all():
        raw = (p.site_legacy or '').strip()
        if not raw:
            continue
        target_name = raw if raw in sites_by_name else LEGACY_SITE_ALIASES.get(raw.lower())
        if target_name:
            p.site_id = sites_by_name[target_name].id
            updated += 1
        else:
            unmapped[raw] = unmapped.get(raw, 0) + 1
    db.session.commit()
    return updated, unmapped


def backfill_devices(sites_by_name):
    rule_counts = {'assigned_person': 0, 'open_loaner': 0, 'description_prefix': 0}

    assigned_assets = {a.asset_tag: a for a in Asset.query.filter(Asset.assigned_to_id.isnot(None)).all()}
    open_loaners = {lc.asset_tag: lc for lc in LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None)).all()}

    for r in AssetRegistry.query.filter(AssetRegistry.site_id.is_(None)).all():
        site_id = None
        rule = None

        asset = assigned_assets.get(r.asset_tag)
        if asset and asset.assigned_to and asset.assigned_to.site_id:
            site_id, rule = asset.assigned_to.site_id, 'assigned_person'

        if not site_id:
            lc = open_loaners.get(r.asset_tag)
            if lc and lc.person_id:
                person = Person.query.get(lc.person_id)
                if person and person.site_id:
                    site_id, rule = person.site_id, 'open_loaner'

        if not site_id and r.description:
            desc = r.description.strip().upper()
            for prefix, site_name in DESCRIPTION_PREFIX_TO_SITE.items():
                if desc.startswith(prefix):
                    site_id, rule = sites_by_name[site_name].id, 'description_prefix'
                    break

        if site_id:
            r.site_id = site_id
            rule_counts[rule] += 1

    db.session.commit()
    still_blank = AssetRegistry.query.filter(AssetRegistry.site_id.is_(None)).count()
    return rule_counts, still_blank


def main():
    with app.app_context():
        sites_by_name = {name: get_or_create_site(name) for name in CANONICAL_SITES}
        db.session.commit()

        people_updated, unmapped = backfill_people(sites_by_name)
        rule_counts, devices_still_blank = backfill_devices(sites_by_name)

        print('=== Site backfill summary ===')
        for name, site in sites_by_name.items():
            print(f'Site: {name} (id={site.id})')
        print(f'People updated: {people_updated}')
        if unmapped:
            print('Unmapped person.site values (left blank, review manually):')
            for val, count in unmapped.items():
                print(f'  {val!r}: {count}')
        print('Devices backfilled by rule:')
        for rule, count in rule_counts.items():
            print(f'  {rule}: {count}')
        print(f'Devices still with no site (needs manual assignment): {devices_still_blank}')


if __name__ == '__main__':
    main()
