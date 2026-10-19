# Resolve the tickets of repairs that were already returned, 2026-10-19. Dry run unless COMMIT=1.
#
# Before repairs and tickets closed together, marking a repair returned left its
# ticket open (production had 9). This resolves each one with a comment saying
# when and how the repair came back. Nobody is emailed and no automations run:
# the repairs are long done, so a "resolved" notice now would just be noise.
# Run inside the web container:
#   ssh docker-host 'docker exec -i [-e COMMIT=1] -w /app asset-tracking-plus-web-1 python -' < this_file
import os, logging
logging.disable(logging.CRITICAL)
import foxdesk.models as M
from foxdesk import app, db

COMMIT = os.environ.get('COMMIT') == '1'
ACTOR = 'Data cleanup (2026-10-19)'

with app.app_context():
    rows = (M.Repair.query.join(M.Ticket, M.Ticket.id == M.Repair.ticket_id)
            .filter(M.Repair.returned_at.isnot(None), M.Ticket.status.in_(('open', 'in_progress')))
            .order_by(M.Repair.returned_at).all())
    for r in rows:
        t = r.ticket
        outcome = M.REPAIR_OUTCOMES.get(r.outcome, r.outcome or 'returned')
        print(f'#{t.id} {t.status:<11} {r.asset_tag:<14} returned {r.returned_at:%Y-%m-%d} ({outcome})  {t.subject[:50]}')
        if COMMIT:
            db.session.add(M.TicketComment(ticket_id=t.id, author_label=ACTOR,
                                           body=f'Resolved: the repair of {r.asset_tag} was returned on '
                                                f'{r.returned_at:%Y-%m-%d} ({outcome}).'))
            t.status, t.resolved_at, t.updated_at = 'resolved', t.resolved_at or r.returned_at, r.returned_at
            db.session.add(M.ActivityLog(actor_type='system', actor_label=ACTOR, actor_user_id=None, site_id=t.site_id,
                                         ticket_id=t.id, action='ticket_status',
                                         summary=f'Ticket #{t.id} resolved: its repair was returned {r.returned_at:%Y-%m-%d}.'))
    if COMMIT:
        db.session.commit()
    print(f'{"Resolved" if COMMIT else "Would resolve"} {len(rows)} ticket(s).')
