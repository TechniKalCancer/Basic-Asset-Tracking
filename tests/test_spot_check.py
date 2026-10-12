"""Spot checks: scanning devices and ID cards, and whether the right student has each device."""
from conftest import A


def lookup(client, code):
    return client.get('/admin/spot_check/lookup', query_string={'code': code}).get_json()


def record(client, code, found=None):
    return client.post('/admin/spot_check/record', json={'code': code, 'found_person_id': found}).get_json()


def test_lookups(client, make):
    ana = make.person('Ana', 'Ruiz', external_id='100234', grad_year=2029)
    row = make.device(tag='FCMS-0042', serial='5PQ2X73', holder=ana)
    hit = lookup(client, 'FCMS-0042')
    assert hit['kind'] == 'device' and hit['expected']['name'] == 'Ana Ruiz' and hit['how'] == 'assigned'
    assert lookup(client, '5pq2x73')['asset_tag'] == 'FCMS-0042', 'serial barcodes work, any case'
    assert lookup(client, 'https://foxdesk.example.org/r/FCMS-0042')['asset_tag'] == 'FCMS-0042', 'a label\'s QR code'
    person = lookup(client, '100234')
    assert person['kind'] == 'person' and person['devices'] == [row.asset_tag]
    assert lookup(client, 'NOPE-999') == {'kind': 'unknown', 'code': 'NOPE-999'}
    people = client.get('/admin/spot_check/people?q=ana ru').get_json()['people']
    assert [p['name'] for p in people] == ['Ana Ruiz']


def test_results_and_problems(client, make):
    ana, bo = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee')
    make.device(tag='T-ANA', holder=ana)
    make.device(tag='T-BO', holder=bo)
    make.device(tag='T-SPARE')
    assert record(client, 'T-ANA')['result'] == 'match', 'tapping "it\'s with Ana" is a match'
    assert record(client, 'T-ANA', found=ana.id)['result'] == 'match'
    wrong = record(client, 'T-ANA', found=bo.id)
    assert wrong['result'] == 'wrong_student' and wrong['expected']['name'] == 'Ana Ruiz' and wrong['found']['devices'] == ['T-BO']
    assert record(client, 'T-SPARE', found=bo.id)['result'] == 'unassigned_in_use'
    assert record(client, 'GHOST-1')['result'] == 'unknown_device'

    page = client.get('/admin/spot_check').get_data(as_text=True)
    assert '5 checked' in page and 'Open problems' in page and '/static/js/scanner.js' in page
    problem = A.SpotCheck.query.filter_by(result='wrong_student').one()
    client.post(f'/admin/spot_check/{problem.id}/resolve')
    assert A.db.session.get(A.SpotCheck, problem.id).resolved_at is not None
    history = client.get('/admin/spot_check/history?show=all&days=1').get_data(as_text=True)
    assert 'Someone else' in history and 'resolved by' in history
    assert 'T-SPARE' in client.get('/admin/spot_check/history/export?show=all').get_data(as_text=True)


def test_loaner_holder_is_who_should_have_it(client, make):
    ana = make.person('Ana', 'Ruiz')
    row = make.device(tag='LOAN-3')
    row.is_loaner = True
    A.db.session.add(A.LoanerCheckout(asset_tag='LOAN-3', person_id=ana.id, person_name=ana.full_name))
    A.db.session.commit()
    hit = lookup(client, 'LOAN-3')
    assert hit['expected']['name'] == 'Ana Ruiz' and hit['how'] == 'loaner'
    assert record(client, 'LOAN-3', found=ana.id)['result'] == 'match'


def test_problem_runs_automation(client, make):
    cat = make.ticket_category('Device collection')
    ana, bo = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee')
    make.device(tag='T-ANA', holder=ana)
    template = A.TEMPLATES_BY_KEY['spotcheck_ticket']['build']()
    A.db.session.add(A.AutomationRule(name='Swap ticket', enabled=True, **template))
    A.db.session.commit()
    record(client, 'T-ANA', found=bo.id)
    t = A.Ticket.query.one()
    assert t.subject == 'Spot check: T-ANA was with Bo Lee' and t.category_id == cat.id
    assert 'someone else\'s device' in t.description and 'Ana Ruiz' in t.description
    record(client, 'T-ANA', found=ana.id)
    assert A.Ticket.query.count() == 1, 'a match runs nothing'
