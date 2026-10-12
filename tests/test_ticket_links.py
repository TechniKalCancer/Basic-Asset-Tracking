"""Merging duplicate tickets and linking related ones."""
from decimal import Decimal

from conftest import A, patch_everywhere, png_bytes


def ticket(cat, subject, email='ana@example.edu', name='Ana Ruiz', **kw):
    t = A.Ticket(category_id=cat.id, subject=subject, description=f'{subject} details', requester_name=name,
                 requester_email=email, **kw)
    A.db.session.add(t)
    A.db.session.commit()
    return t


def test_merge_moves_everything_and_tells_the_requester(client, make, sent_emails):
    cat = make.ticket_category()
    keep = ticket(cat, 'Screen cracked')
    dupe = ticket(cat, 'Broken screen again', asset_tag='T00042')
    A.db.session.add_all([A.TicketComment(ticket_id=dupe.id, body='It got worse', author_label='Ana'),
                          A.TicketCharge(ticket_id=dupe.id, description='Screen', amount=Decimal('40.00')),
                          A.Attachment(owner_type='ticket', owner_id=dupe.id, filename='crack.png', content_type='image/png',
                                       size_bytes=len(png_bytes()), data=png_bytes())])
    A.db.session.commit()

    r = client.post(f'/admin/tickets/{dupe.id}/merge', data={'direction': 'into', 'other': f'#{keep.id}', 'notify': 'on'},
                    follow_redirects=True)
    assert f'Merged ticket #{dupe.id} into #{keep.id}'.encode() in r.data
    keep, dupe = A.db.session.get(A.Ticket, keep.id), A.db.session.get(A.Ticket, dupe.id)
    assert (dupe.status, dupe.merged_into_id) == ('closed', keep.id)
    assert [c.body for c in keep.comments][0] == 'It got worse' and 'Merged ticket' in keep.comments[-1].body
    assert len(keep.charges) == 1 and keep.asset_tag == 'T00042'
    assert A.Attachment.query.filter_by(owner_type='ticket', owner_id=keep.id).count() == 1
    to, subject, body = sent_emails[-1]
    assert to == 'ana@example.edu' and f'#{dupe.id}' in subject and f'ticket #{keep.id}' in body

    r = client.post(f'/admin/tickets/{dupe.id}/merge', data={'direction': 'into', 'other': keep.id}, follow_redirects=True)
    assert b'already merged' in r.data
    page = client.get(f'/admin/tickets/{dupe.id}').get_data(as_text=True)
    assert 'Merged into' in page and 'Merge or link' not in page


def test_merge_guards(client, make):
    cat = make.ticket_category()
    a, b = ticket(cat, 'One'), ticket(cat, 'Two')
    r = client.post(f'/admin/tickets/{a.id}/merge', data={'other': a.id}, follow_redirects=True)
    assert b'into itself' in r.data
    r = client.post(f'/admin/tickets/{a.id}/merge', data={'other': '99999'}, follow_redirects=True)
    assert b'Enter the number of a ticket' in r.data
    make.device(tag='R1')
    A.db.session.add(A.Repair(asset_tag='R1', issue_description='x', ticket_id=b.id))
    A.db.session.commit()
    r = client.post(f'/admin/tickets/{b.id}/merge', data={'direction': 'into', 'other': a.id}, follow_redirects=True)
    assert b'tracks a repair' in r.data
    r = client.post(f'/admin/tickets/{a.id}/merge', data={'direction': 'from', 'other': b.id}, follow_redirects=True)
    assert b'tracks a repair' in r.data
    client.post(f'/admin/tickets/{b.id}/merge', data={'direction': 'from', 'other': a.id})
    assert A.db.session.get(A.Ticket, a.id).merged_into_id == b.id, '"merge #… into this one" keeps this ticket'


def test_email_reply_to_a_merged_ticket_lands_on_the_kept_one(make, monkeypatch, sent_emails):
    from test_email_tickets import mailbox, raw_email
    cat = make.ticket_category()
    keep = ticket(cat, 'Wi-Fi', email='bo@example.edu', name='Bo')
    dupe = ticket(cat, 'No internet')
    A.merge_tickets(dupe, keep, 'tech')
    A.db.session.commit()
    mailbox(monkeypatch, raw_email('Ana <ana@example.edu>', f'Re: Update [Ticket #{dupe.id}]', 'Still down', '<x1@ex>'))
    A.check_mailbox()
    record = A.InboundEmail.query.one()
    assert (record.outcome, record.ticket_id) == ('reply', keep.id), 'the duplicate\'s requester can still reply'


def test_link_and_resolve_together(client, make, sent_emails):
    cat = make.ticket_category()
    main = ticket(cat, 'Wi-Fi down in B-wing', email='lead@example.edu', name='Lead')
    first = ticket(cat, 'No Wi-Fi room 204', email='t1@example.edu', name='Teacher One')
    second = ticket(cat, 'Internet out', email='t2@example.edu', name='Teacher Two', status='in_progress')
    client.post(f'/admin/tickets/{first.id}/link', data={'main': main.id})
    r = client.post(f'/admin/tickets/{second.id}/link', data={'main': first.id}, follow_redirects=True)
    assert f'linked under #{main.id}'.encode() in r.data, 'linking under a linked ticket uses its main ticket'
    r = client.post(f'/admin/tickets/{main.id}/link', data={'main': first.id}, follow_redirects=True)
    assert b'already has tickets linked' in r.data

    page = client.get(f'/admin/tickets/{main.id}').get_data(as_text=True)
    assert 'Linked tickets' in page and 'Resolve 2 linked tickets' in page
    assert '2 linked reports' in client.get('/admin/tickets').get_data(as_text=True)
    sent_emails.clear()
    r = client.post(f'/admin/tickets/{main.id}/resolve_linked',
                    data={'message': 'The access point was replaced.', 'email_requesters': 'on', 'include_main': 'on'},
                    follow_redirects=True)
    assert b'Resolved 3 ticket(s); 3 requester(s) emailed.' in r.data
    assert {t.status for t in A.Ticket.query} == {'resolved'}
    assert sorted(to for to, _, _ in sent_emails) == ['lead@example.edu', 't1@example.edu', 't2@example.edu']
    assert all('access point was replaced' in body for _, _, body in sent_emails)
    assert all(t.first_response_at for t in A.Ticket.query)

    client.post(f'/admin/tickets/{first.id}/unlink')
    assert A.db.session.get(A.Ticket, first.id).parent_id is None


def test_portal_follows_a_merge(anon_client, make, sent_emails):
    from test_portal import sign_in, turn_on
    turn_on('portal', 'tickets')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org')
    cat = make.ticket_category()
    keep = ticket(cat, 'Keyboard', email='ana@students.org', requester_person_id=kid.id)
    dupe = ticket(cat, 'Keys stuck', email='ana@students.org', requester_person_id=kid.id)
    A.merge_tickets(dupe, keep, 'tech')
    A.db.session.commit()
    sign_in(anon_client, 'ana@students.org', sent_emails)
    r = anon_client.get(f'/my/tickets/{dupe.id}')
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/my/tickets/{keep.id}')
