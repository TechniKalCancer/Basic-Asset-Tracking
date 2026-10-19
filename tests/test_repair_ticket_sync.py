"""A repair and its ticket close together, link to each other both ways, and a
repair started from a device with an open ticket goes on that ticket."""
from conftest import A


def category(make):
    return A.TicketCategory.query.filter_by(name='Hardware').first() or make.ticket_category()


def ticket_for(make, row, subject='Screen cracked'):
    t = A.Ticket(category_id=category(make).id, subject=subject, description='x', asset_tag=row.asset_tag,
                 requester_name='Ana Ruiz', requester_email='ana@students.org')
    A.db.session.add(t)
    A.db.session.commit()
    return t


def send(client, row, **form):
    data = dict(issue_description="Won't power on")
    data.update(form)
    client.post(f'/admin/assets/{row.asset_tag}/repairs/send', data=data)
    return A.Repair.query.filter_by(asset_tag=row.asset_tag).order_by(A.Repair.id.desc()).first()


def test_repair_goes_on_the_devices_open_ticket(client, make):
    row = make.device()
    t = ticket_for(make, row)
    repair = send(client, row)
    assert repair.ticket_id == t.id and A.Ticket.query.count() == 1, 'no second ticket'
    assert any('Sent' in c.body and 'to repair' in c.body for c in A.TicketComment.query.filter_by(ticket_id=t.id))
    other = make.device()
    assert send(client, other).ticket_id != t.id, 'a device with no open ticket gets its own'


def test_mark_returned_resolves_the_ticket(client, make):
    holder = make.person('Ana', 'Ruiz')
    row = make.device(holder=holder)
    repair = send(client, row)
    r = client.post(f'/admin/repairs/{repair.id}/return', data={'outcome': 'fixed', 'notes': 'New screen'}, follow_redirects=True)
    assert b'is resolved too' in r.data
    ticket = A.db.session.get(A.Ticket, repair.ticket_id)
    assert ticket.status == 'resolved' and ticket.resolved_at is not None
    assert A.Asset.query.filter_by(asset_tag=row.asset_tag).one().status == 'assigned'
    assert A.RepairNote.query.filter(A.RepairNote.body.like('Returned: Fixed%')).count() == 1


def test_resolving_the_ticket_closes_the_repair(client, make):
    row = make.device()
    repair = send(client, row)
    url = f'/admin/tickets/{repair.ticket_id}/status'
    page = client.get(f'/admin/tickets/{repair.ticket_id}').get_data(as_text=True)
    assert 'Repair outcome' in page and f'href="/admin/repairs/{repair.id}"' in page

    r = client.post(url, data={'status': 'resolved', 'priority': 'normal'}, follow_redirects=True)
    assert b'Pick how the repair ended' in r.data
    assert A.db.session.get(A.Ticket, repair.ticket_id).status == 'open' and A.db.session.get(A.Repair, repair.id).returned_at is None

    r = client.post(url, data={'status': 'closed', 'priority': 'normal', 'repair_outcome': 'could_not_repair',
                               'repair_notes': 'Board is dead'}, follow_redirects=True)
    assert b'repair of' in r.data and b'is closed (Could Not Repair)' in r.data
    repair = A.db.session.get(A.Repair, repair.id)
    assert repair.outcome == 'could_not_repair' and repair.notes == 'Board is dead' and repair.returned_at
    assert A.db.session.get(A.Ticket, repair.ticket_id).status == 'closed'
    assert A.Asset.query.filter_by(asset_tag=row.asset_tag).one().status == 'retired'

    r = client.post(url, data={'status': 'open', 'priority': 'normal'}, follow_redirects=True)
    assert A.db.session.get(A.Repair, repair.id).returned_at is not None, 'reopening the ticket leaves the repair closed'


def test_link_both_ways(client, make):
    row = make.device()
    repair = send(client, row)
    old_ticket = repair.ticket_id
    first_report = A.Ticket(category_id=make.ticket_category('Other').id, subject='Kid says it is broken', description='x')
    A.db.session.add(first_report)
    A.db.session.commit()

    page = client.get(f'/admin/tickets/{first_report.id}').get_data(as_text=True)
    assert 'Link an open repair by device' in page
    r = client.post(f'/admin/tickets/{first_report.id}/link_repair', data={'device': row.asset_tag}, follow_redirects=True)
    assert b'is on this ticket now' in r.data and b'still open' in r.data
    repair = A.db.session.get(A.Repair, repair.id)
    assert repair.ticket_id == first_report.id and A.db.session.get(A.Ticket, first_report.id).asset_tag == row.asset_tag
    assert any('moved to ticket' in c.body for c in A.TicketComment.query.filter_by(ticket_id=old_ticket))

    detail = client.get(f'/admin/repairs/{repair.id}').get_data(as_text=True)
    assert f'href="/admin/tickets/{first_report.id}"' in detail and 'Kid says it is broken' in detail
    r = client.post(f'/admin/repairs/{repair.id}/link_ticket', data={'ticket_id': f'#{old_ticket}'}, follow_redirects=True)
    assert A.db.session.get(A.Repair, repair.id).ticket_id == old_ticket

    other = send(client, make.device())
    r = client.post(f'/admin/repairs/{other.id}/link_ticket', data={'ticket_id': str(old_ticket)}, follow_redirects=True)
    assert b'already has a repair' in r.data
    assert b'No ticket #999' in client.post(f'/admin/repairs/{other.id}/link_ticket', data={'ticket_id': '999'},
                                            follow_redirects=True).data


def test_ticket_offers_the_devices_repair_or_sends_it(client, make):
    row = make.device()
    repair = send(client, row)
    second = ticket_for(make, row, 'Same Chromebook, parent called')
    page = client.get(f'/admin/tickets/{second.id}').get_data(as_text=True)
    assert 'Put that repair on this ticket' in page
    client.post(f'/admin/tickets/{second.id}/link_repair', data={'repair_id': repair.id})
    assert A.db.session.get(A.Repair, repair.id).ticket_id == second.id

    fresh = make.device()
    t = ticket_for(make, fresh, 'Keyboard missing keys')
    assert 'Send to Repair' in client.get(f'/admin/tickets/{t.id}').get_data(as_text=True)
    r = client.post(f'/admin/tickets/{t.id}/send_to_repair', data={'issue_description': 'Keys missing'}, follow_redirects=True)
    assert b'on its ticket #' in r.data
    sent = A.Repair.query.filter_by(asset_tag=fresh.asset_tag).one()
    assert sent.ticket_id == t.id and A.Asset.query.filter_by(asset_tag=fresh.asset_tag).one().status == 'repair'


def test_resolve_linked_keeps_open_repairs_open(client, make):
    main = A.Ticket(category_id=category(make).id, subject='Cart outage', description='x')
    A.db.session.add(main)
    A.db.session.commit()
    repair = send(client, make.device())
    plain = ticket_for(make, make.device(), 'Also broken')
    for t in (A.db.session.get(A.Ticket, repair.ticket_id), plain):
        t.parent_id = main.id
    A.db.session.commit()
    r = client.post(f'/admin/tickets/{main.id}/resolve_linked', data={'message': 'Fixed the cart.'}, follow_redirects=True)
    assert b'Resolved 1 ticket' in r.data and b'Kept open: #' + str(repair.ticket_id).encode() in r.data
    assert A.db.session.get(A.Ticket, repair.ticket_id).status == 'open' and plain.status == 'resolved'


def test_automation_repair_from_a_ticket_stays_on_it(make):
    from foxdesk.services.helpdesk import _send_device_to_repair
    row = make.device()
    t = ticket_for(make, row)
    status, message = _send_device_to_repair(row.asset_tag, None, None, 'From automation', None, ticket=t)
    A.db.session.commit()
    assert status == 'ok' and A.Repair.query.filter_by(asset_tag=row.asset_tag).one().ticket_id == t.id
