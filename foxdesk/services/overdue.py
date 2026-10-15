"""What's overdue at each school — loaners past due, repairs past the repair
time limit, assigned devices past their due date — and the daily email that
sends each school's list to its techs (Site.techs).

Repairs: every repair is due back within OverdueSettings.repair_max_days
(two weeks by default) of being sent. A repair with no expected-return date
is due at that limit; a later date can't be set (repair_limit_problem).

The email goes out once a school day at alert_hour local time, using the
school calendar from the SLA settings (weekends and closed dates skipped,
whether or not SLAs themselves are on). Students are never emailed from
here; a repair loaner's student isn't emailed at all (see
_send_overdue_loaner_reminders) since the wait is on the repair, not them.
"""
from collections import OrderedDict
from datetime import timedelta

from foxdesk.clock import utcnow
from foxdesk.core import APP_URL, EMAIL_ENABLED, db, logger
from foxdesk.models import (
    AssetRegistry, AssignmentHistory, LoanerCheckout, OverdueSettings, REPAIR_STATUSES, Repair, Site, User,
)
from foxdesk.services.emailer import send_email

DEFAULT_REPAIR_MAX_DAYS = 14


def overdue_settings(create=False):
    """The settings row; until it's first saved, the defaults."""
    row = db.session.get(OverdueSettings, 1)
    if row is None:
        row = OverdueSettings(id=1, repair_max_days=DEFAULT_REPAIR_MAX_DAYS, alerts_enabled=True, alert_hour=7)
        if create:
            db.session.add(row)
    return row


def repair_max_days():
    return overdue_settings().repair_max_days or DEFAULT_REPAIR_MAX_DAYS


def repair_limit_date(sent_at, max_days=None):
    return sent_at.date() + timedelta(days=max_days or repair_max_days())


def repair_due_date(repair, max_days=None):
    """When the repair should be back: its expected date, else the limit."""
    return repair.expected_return_at or repair_limit_date(repair.sent_at, max_days)


def repair_limit_problem(sent_at, expected_return_at):
    """Why this expected-return date isn't allowed, or None."""
    if expected_return_at is None:
        return None
    limit = repair_limit_date(sent_at or utcnow())
    if expected_return_at > limit:
        return (f'Repairs can be out at most {repair_max_days()} days — the latest expected return is '
                f'{limit:%Y-%m-%d}. Change the limit under Admin → Reminders if a vendor needs longer.')
    return None


def _site_ids_for(tags):
    return dict(AssetRegistry.query.filter(AssetRegistry.asset_tag.in_(tags))
                .with_entities(AssetRegistry.asset_tag, AssetRegistry.site_id)) if tags else {}


def overdue_by_site(site_ids=None, today=None):
    """{site_id or None: dict(site, loaners, repairs, assignments)} with only
    the schools that have something overdue; None is devices with no school.
    site_ids=None means every school."""
    today = today or utcnow().date()
    max_days = repair_max_days()

    loaners = LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None), LoanerCheckout.due_date.isnot(None),
                                          LoanerCheckout.due_date < today).all()
    repairs = [r for r in Repair.query.filter(Repair.returned_at.is_(None)).all() if repair_due_date(r, max_days) < today]
    assignments = AssignmentHistory.query.filter(AssignmentHistory.unassigned_at.is_(None),
                                                 AssignmentHistory.due_date.isnot(None),
                                                 AssignmentHistory.due_date < today).all()
    repair_loaners = {l.repair_id: l for l in LoanerCheckout.query.filter(
        LoanerCheckout.checked_in_at.is_(None), LoanerCheckout.repair_id.in_([r.id for r in repairs]))} if repairs else {}
    site_of = _site_ids_for({x.asset_tag for x in loaners + repairs + assignments})

    out = OrderedDict()
    sites = {s.id: s for s in Site.query.order_by(Site.name)}

    def bucket(tag):
        sid = site_of.get(tag)
        if site_ids is not None and sid not in site_ids:
            return None
        if sid not in out:
            out[sid] = dict(site=sites.get(sid), loaners=[], repairs=[], assignments=[])
        return out[sid]

    for l in sorted(loaners, key=lambda l: l.due_date):
        b = bucket(l.asset_tag)
        if b is not None:
            b['loaners'].append(dict(tag=l.asset_tag, person=l.person_name, person_id=l.person_id, due=l.due_date,
                                     days=(today - l.due_date).days, for_repair=bool(l.repair_id)))
    for r in sorted(repairs, key=lambda r: r.sent_at):
        b = bucket(r.asset_tag)
        if b is not None:
            due = repair_due_date(r, max_days)
            loaner = repair_loaners.get(r.id)
            b['repairs'].append(dict(id=r.id, tag=r.asset_tag, person=r.person_name_snapshot, due=due,
                                     days=(today - due).days, days_open=(today - r.sent_at.date()).days,
                                     status=REPAIR_STATUSES.get(r.status, r.status),
                                     loaner=f'{loaner.asset_tag} with {loaner.person_name}' if loaner else None))
    for a in sorted(assignments, key=lambda a: a.due_date):
        b = bucket(a.asset_tag)
        if b is not None:
            b['assignments'].append(dict(tag=a.asset_tag, person=a.person_name, person_id=a.person_id, due=a.due_date,
                                         days=(today - a.due_date).days))
    return OrderedDict(sorted(out.items(), key=lambda kv: (kv[1]['site'] is None, kv[1]['site'].name if kv[1]['site'] else '')))


def _count(section):
    return len(section['loaners']) + len(section['repairs']) + len(section['assignments'])


def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


def digest_text(sections, today=None):
    """(subject, body) for one tech's schools."""
    today = today or utcnow().date()
    total = sum(_count(s) for s in sections)
    subject = f'FoxDesk: {_plural(total, "overdue item")} ({today:%a %b} {today.day})'
    lines = []
    for s in sections:
        name = s['site'].name if s['site'] else 'Devices with no school set'
        lines += ['', f'{name} — {_count(s)} overdue', '=' * (len(name) + 14)]
        if s['repairs']:
            lines.append(f'Repairs past due ({len(s["repairs"])}):')
            for r in s['repairs']:
                extra = f'; loaner {r["loaner"]}' if r['loaner'] else ''
                lines.append(f'  - {r["tag"]}  {r["status"]}, out {r["days_open"]} days, due {r["due"]:%b} {r["due"].day}'
                             f' ({_plural(r["days"], "day")} late){" — " + r["person"] if r["person"] else ""}{extra}')
        if s['loaners']:
            lines.append(f'Loaners past due ({len(s["loaners"])}):')
            for l in s['loaners']:
                lines.append(f'  - {l["tag"]}  {l["person"]}, due {l["due"]:%b} {l["due"].day} ({_plural(l["days"], "day")} late)'
                             + (' — repair loaner, student not emailed' if l['for_repair'] else ''))
        if s['assignments']:
            lines.append(f'Assigned devices past their due date ({len(s["assignments"])}):')
            for a in s['assignments']:
                lines.append(f'  - {a["tag"]}  {a["person"]}, due {a["due"]:%b} {a["due"].day} ({_plural(a["days"], "day")} late)')
    if APP_URL:
        lines += ['', f'Full list: {APP_URL}/admin/overdue']
    lines += ['', 'You get this because you\'re a tech for these schools (Admin → Sites). It only comes on school days '
                  'when something is overdue.']
    return subject, '\n'.join(lines).lstrip('\n')


def _recipients(by_site):
    """{user: [sections]} — each school's techs, and super admins for devices with no school."""
    plan = OrderedDict()
    for sid, section in by_site.items():
        if sid is None:
            users = User.query.filter_by(is_super_admin=True, is_active=True).all()
        else:
            users = [u for u in section['site'].techs if u.is_active] if section['site'] else []
        for u in users:
            if u.email:
                plan.setdefault(u, []).append(section)
    return plan


def send_tech_alerts(only_user=None, today=None):
    """Emails each tech their schools' overdue list now. only_user sends just
    that person's (the Reminders page's "Send me mine now"). Returns
    (emails sent, failed, techs with nothing overdue or no email)."""
    by_site = overdue_by_site(today=today)
    plan = _recipients(by_site)
    if only_user is not None:
        plan = {u: s for u, s in plan.items() if u.id == only_user.id}
    sent = failed = 0
    for user, sections in plan.items():
        subject, body = digest_text(sections, today)
        try:
            send_email(user.email, subject, body)
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Overdue email to %s failed: %s', user.email, e)
    return sent, failed


def alert_due_now(now=None):
    """True when today's email should go out: on, a school day, past the hour, not sent yet."""
    from foxdesk.services.sla import Calendar, sla_settings
    row = overdue_settings()
    if not row.alerts_enabled:
        return False
    cal = Calendar(sla_settings())
    local = cal.local(now or utcnow())
    if not cal.is_school_day(local.date()) or local.hour < (row.alert_hour or 0):
        return False
    return row.last_alert_on is None or row.last_alert_on < local.date()


def maybe_send_daily(now=None):
    """Called every few minutes by the background loop. Claims the day with a
    conditional UPDATE first, so only one gunicorn worker sends."""
    if not EMAIL_ENABLED or not alert_due_now(now):
        return False
    from foxdesk.services.sla import Calendar, sla_settings
    today = Calendar(sla_settings()).local(now or utcnow()).date()
    if db.session.get(OverdueSettings, 1) is None:
        db.session.add(overdue_settings())
        db.session.commit()
    claimed = (OverdueSettings.query
               .filter(OverdueSettings.id == 1,
                       db.or_(OverdueSettings.last_alert_on.is_(None), OverdueSettings.last_alert_on < today))
               .update({'last_alert_on': today}, synchronize_session=False))
    db.session.commit()
    if not claimed:
        return False
    sent, failed = send_tech_alerts(today=today)
    if sent or failed:
        logger.info('Daily overdue email: %s sent, %s failed.', sent, failed)
    return True
