"""Active/inactive beside people's names: the person page's big badge and the
small marker everywhere else, including history rows that only keep an id."""
from conftest import A

ACTIVE = '<span class="ps ps-on" title="Active">'
INACTIVE = '<span class="ps ps-off" title="Inactive: graduated or left">'


def test_person_page_big_badge(client, make):
    ana = make.person('Ana', 'Ruiz')
    gone = make.person('Old', 'Grad', active=False)
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'class="ps-lg ps-on"' in page and 'Marked inactive' not in page
    page = client.get(f'/admin/people/{gone.id}').get_data(as_text=True)
    assert 'class="ps-lg ps-off"' in page and 'Inactive</span>' in page and 'Marked inactive' in page


def test_marker_beside_names_across_pages(client, make):
    ana = make.person('Ana', 'Ruiz')
    gone = make.person('Old', 'Ruiz', active=False)
    cb = make.device(tag='CB-1', holder=gone)
    A.db.session.add(A.AssignmentHistory(asset_tag=cb.asset_tag, person_id=ana.id, person_name=ana.full_name))
    A.db.session.commit()

    people = client.get('/admin/people?show=all').get_data(as_text=True)
    assert ACTIVE in people and INACTIVE in people
    search = client.get('/admin/search?q=Ruiz').get_data(as_text=True)
    assert search.count(INACTIVE) >= 2, 'the person row and "Assigned To" on their device'
    assert ACTIVE in search and '<span class="ps-sr">Active</span>' in search, 'active is named for screen readers'
    device = client.get(f'/admin/assets/{cb.asset_tag}/assign').get_data(as_text=True)
    assert INACTIVE in device, 'currently assigned to someone inactive'
    assert ACTIVE in device, 'a history row resolves its person id'
    assert INACTIVE in client.get('/admin/registry').get_data(as_text=True)


def test_ticket_requester_status(client, make):
    gone = make.person('Old', 'Grad', active=False)
    t = A.Ticket(category_id=make.ticket_category().id, subject='Broken hinge', description='x',
                 requester_name=gone.full_name, requester_email=gone.email,
                 requester_person_id=gone.id)
    A.db.session.add(t)
    A.db.session.commit()
    assert INACTIVE in client.get('/admin/tickets').get_data(as_text=True)
    assert INACTIVE in client.get(f'/admin/tickets/{t.id}').get_data(as_text=True)
