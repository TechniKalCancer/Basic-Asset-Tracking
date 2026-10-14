"""The person page (everything they've had), and agreement checkboxes on People."""
from datetime import date, timedelta

from conftest import A
from foxdesk.services.agreements import short_name
from foxdesk.clock import utcnow

AUP = 'Students and users will follow the acceptable use rules for school technology.'


def agreement(client, title, short=''):
    client.post('/admin/agreements/new', data={'title': title, 'short_name': short, 'body': AUP + ' ' + title,
                                               'require_student': 'on', 'require_guardian': 'on'})
    return A.Agreement.query.filter_by(title=title).one()


def test_person_page_shows_everything_they_have_had(client, make):
    ana = make.person('Ana', 'Ruiz', grad_year=2029, external_id='100234')
    cb_now = make.device(tag='CB-NOW', holder=ana)
    old = make.device(tag='CB-OLD')
    charger = make.device(tag='CHG-1', device_type='charger')
    loaner = make.device(tag='LOAN-1')
    long_ago = utcnow() - timedelta(days=200)
    A.db.session.add_all([
        A.AssignmentHistory(asset_tag=old.asset_tag, person_id=ana.id, person_name=ana.full_name, assigned_at=long_ago,
                            unassigned_at=long_ago + timedelta(days=90), condition_in='Cracked corner'),
        A.AssignmentHistory(asset_tag=charger.asset_tag, person_id=ana.id, person_name=ana.full_name, assigned_at=long_ago),
        A.LoanerCheckout(asset_tag=loaner.asset_tag, person_id=ana.id, person_name=ana.full_name,
                         checked_out_at=utcnow() - timedelta(days=30), due_date=date.today() - timedelta(days=25),
                         checked_in_at=utcnow() - timedelta(days=20)),
        A.LoanerCheckout(asset_tag=loaner.asset_tag, person_id=ana.id, person_name=ana.full_name,
                         checked_out_at=utcnow() - timedelta(days=2), due_date=date.today() + timedelta(days=3)),
        A.Incident(asset_tag=old.asset_tag, person_id=ana.id, person_name=ana.full_name, description='Cracked screen',
                   fee_charged=True, fee_amount=40),
    ])
    A.db.session.commit()
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Class of 2029' in page and 'ID 100234' in page
    has_now = page[page.index('Has now'):page.index('Agreements', page.index('Has now'))]
    assert 'CB-NOW' in has_now and 'LOAN-1' in has_now and 'Loaner' in has_now
    chromebooks = page[page.index('Chromebook history'):page.index('id="history"') + 2000]
    assert 'CB-OLD' in chromebooks and '90 days' in chromebooks and 'Cracked corner' in chromebooks
    loaners = page[page.index('Loaners <span class="badge'):page.index('Other devices <span')]
    assert loaners.count('<code>LOAN-1</code>') == 2 and 'late' in loaners
    others = page[page.index('Other devices <span'):page.index('Damage and fees')]
    assert 'CHG-1' in others and 'CB-OLD' not in others
    assert 'Cracked screen' in page and '$40.00 owed' in page
    r = client.get(f'/admin/people/{ana.id}/history')
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/admin/people/{ana.id}#history')
    assert f'href="/admin/people/{ana.id}"' in client.get(f'/admin/assets/{cb_now.asset_tag}/assign').get_data(as_text=True)


def test_agreement_checkboxes_on_people(client, make):
    ana, bo = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee')
    staff = make.person('Pat', 'Teach', role='staff')
    aup = agreement(client, 'Acceptable Use Agreement for Technology')
    cb = agreement(client, 'Technology & Chromebook Agreement')
    assert (short_name(aup), short_name(cb)) == ('AUP', 'CB')

    page = client.get('/admin/people').get_data(as_text=True)
    assert '>AUP</th>' in page and '>CB</th>' in page and page.count('class="ag-check"') == 4, 'students only'
    url = f'/admin/agreements/{aup.id}/check/{ana.id}'
    data = client.post(url, json={'action': 'sign'}).get_json()
    assert data['complete'] and data['signed_by'] == 'Ana Ruiz'
    sig = A.AgreementSignature.query.one()
    assert sig.signer_role == 'paper' and sig.recorded_by

    unsigned = client.get(f'/admin/people?unsigned={aup.id}').get_data(as_text=True)
    assert 'Bo Lee' in unsigned and 'Ana Ruiz' not in unsigned and 'Pat Teach' not in unsigned
    assert client.post(url, json={'action': 'undo'}).get_json()['complete'] is False
    assert A.AgreementSignature.query.count() == 0

    A.sign(aup, bo, 'student', 'Bo Lee')
    A.sign(aup, bo, 'guardian', 'Kim Lee')
    A.db.session.commit()
    r = client.post(f'/admin/agreements/{aup.id}/check/{bo.id}', json={'action': 'undo'})
    assert r.status_code == 400 and A.AgreementSignature.query.count() == 2, 'signatures given online stay'
    assert 'ag-check-static' in client.get(f'/admin/people/{bo.id}').get_data(as_text=True)
    assert client.post(f'/admin/agreements/{aup.id}/check/{staff.id}', json={'action': 'sign'}).status_code == 400


def test_bulk_paper_copies(client, make):
    ana, bo, cy = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee'), make.person('Cy', 'Day')
    cb = agreement(client, 'Technology & Chromebook Agreement', short='CB')
    page = client.get(f'/admin/agreements/{cb.id}').get_data(as_text=True)
    assert 'Signed on paper' in page and page.count('class="ad-pick"') == 3
    r = client.post(f'/admin/agreements/{cb.id}/paper_bulk', data={'person_id': [ana.id, bo.id]}, follow_redirects=True)
    assert b'Recorded 2 signed paper copies' in r.data
    assert A.status_for(cb, A.db.session.get(A.Person, ana.id))['complete']
    assert not A.status_for(cb, A.db.session.get(A.Person, cy.id))['complete']
    r = client.post(f'/admin/agreements/{cb.id}/paper_bulk', data={'person_id': [ana.id]}, follow_redirects=True)
    assert b'Recorded 0 signed paper copies' in r.data, 'already signed ones are skipped'
