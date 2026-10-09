"""Queues, teams, saved replies, signatures, and "someone else is on this ticket"."""
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from conftest import A


def tech(username, **kw):
    """A district-wide tech (a site-scoped user only sees tickets at their own schools)."""
    u = A.User(username=username, password_hash=generate_password_hash('pw', method='pbkdf2:sha256'),
               can_tickets=True, is_active=True, is_super_admin=True, **kw)
    A.db.session.add(u)
    A.db.session.commit()
    return u


def login(app_client, username):
    c = A.app.test_client()
    assert c.post('/admin/login', data={'username': username, 'password': 'pw'}).status_code == 302
    return c


def ticket(make_cat, subject='Help', **kw):
    t = A.Ticket(category_id=make_cat.id, subject=subject, description='d', requester_name='Ana Ruiz',
                 requester_email='ana@example.edu', **kw)
    A.db.session.add(t)
    A.db.session.commit()
    return t


def test_queues_and_teams(client, make):
    cat = make.ticket_category()
    jo, al = tech('jo'), tech('al')
    team = A.Team(name='FCHS techs', members=[jo])
    A.db.session.add(team)
    A.db.session.commit()
    mine = ticket(cat, 'SUBJ-mine', assigned_to_user_id=jo.id)
    teams = ticket(cat, 'SUBJ-team', team_id=team.id)
    nobody = ticket(cat, 'SUBJ-nobody')
    old = ticket(cat, 'SUBJ-old', assigned_to_user_id=al.id, updated_at=datetime.utcnow() - timedelta(days=5))
    ticket(cat, 'SUBJ-done', status='resolved')

    c = login(client, 'jo')
    page = lambda view: c.get(f'/admin/tickets?view={view}').get_data(as_text=True)  # noqa: E731
    assert 'SUBJ-mine' in page('mine') and 'SUBJ-team' not in page('mine')
    assert 'SUBJ-team' in page('my_teams') and 'SUBJ-nobody' not in page('my_teams')
    unassigned = page('unassigned')
    assert 'SUBJ-nobody' in unassigned and 'SUBJ-mine' not in unassigned and 'SUBJ-team' not in unassigned
    assert 'SUBJ-old' in page('stale') and 'SUBJ-nobody' not in page('stale')
    assert 'SUBJ-done' not in page('')

    client.post(f'/admin/tickets/{nobody.id}/assign', data={'assigned_to_user_id': '', 'team_id': str(team.id)})
    assert A.Ticket.query.get(nobody.id).team_id == team.id
    assert 'SUBJ-nobody' in client.get(f'/admin/tickets?team_id={team.id}').get_data(as_text=True)
    log = A.ActivityLog.query.filter_by(ticket_id=nobody.id, action='ticket_assign').one()
    assert 'nobody → FCHS techs' in log.summary
    assert mine and teams and old


def test_saved_replies_and_signature(client, make, sent_emails):
    cat = make.ticket_category()
    jo = tech('jo')
    tech('al')
    t = ticket(cat, 'Charger', asset_tag='T123')
    jo_client, al_client = login(client, 'jo'), login(client, 'al')
    jo_client.post('/admin/canned_replies/new', data={'title': 'Try a reset', 'body': 'Hi {first_name}, re #{ticket_id} ({asset_tag}): hold refresh + power. {tech_name}'})
    jo_client.post('/admin/canned_replies/new', data={'title': 'Jo only', 'body': 'x', 'personal': 'on'})
    shared, personal = A.CannedReply.query.order_by(A.CannedReply.id).all()
    assert shared.user_id is None and personal.user_id == jo.id
    assert 'Jo only' not in al_client.get(f'/admin/tickets/{t.id}').get_data(as_text=True), 'personal replies stay personal'

    r = al_client.get(f'/admin/tickets/{t.id}/canned/{shared.id}')
    assert r.get_json()['text'] == f'Hi Ana, re #{t.id} (T123): hold refresh + power. al'
    assert A.CannedReply.query.get(shared.id).use_count == 1
    assert al_client.get(f'/admin/tickets/{t.id}/canned/{personal.id}').status_code == 404

    al_client.post('/admin/canned_replies', data={'action': 'signature', 'signature': 'Al\nDistrict IT'})
    al_client.post(f'/admin/tickets/{t.id}/comment', data={'body': 'Fixed it', 'email_requester': 'on'})
    to, subject, body = sent_emails[-1]
    assert to == 'ana@example.edu' and 'Fixed it\n\n-- \nAl\nDistrict IT' in body
    assert A.TicketComment.query.filter_by(ticket_id=t.id).one().body == 'Fixed it', 'signature not stored on the comment'


def test_presence(client, make):
    cat = make.ticket_category()
    tech('jo'), tech('al')
    t = ticket(cat)
    jo_client, al_client = login(client, 'jo'), login(client, 'al')
    assert jo_client.post(f'/admin/tickets/{t.id}/presence', data={'typing': '0'}).get_json() == {'others': []}
    others = al_client.post(f'/admin/tickets/{t.id}/presence', data={'typing': '1'}).get_json()['others']
    assert others == [{'name': 'jo', 'typing': False}]
    assert jo_client.post(f'/admin/tickets/{t.id}/presence', data={'typing': '0'}).get_json()['others'] == [{'name': 'al', 'typing': True}]
    al_client.post(f'/admin/tickets/{t.id}/presence', data={'leaving': '1'})
    assert jo_client.post(f'/admin/tickets/{t.id}/presence', data={'typing': '0'}).get_json() == {'others': []}
    A.TicketPresence.query.update({'updated_at': datetime.utcnow() - timedelta(minutes=2)})
    A.db.session.commit()
    assert al_client.post(f'/admin/tickets/{t.id}/presence', data={}).get_json() == {'others': []}, 'stale heartbeats ignored'


def test_automation_assigns_team(client, make):
    cat = make.ticket_category()
    team = A.Team(name='Middle school')
    A.db.session.add(team)
    A.db.session.commit()
    A.db.session.add(A.AutomationRule(name='Route', trigger='ticket.created', conditions=[], enabled=True,
                                      actions=[{'type': 'assign_team', 'params': {'value': str(team.id)}}]))
    A.db.session.commit()
    client.post('/admin/tickets/new', content_type='multipart/form-data',
                data={'requester_name': 'X', 'category_id': str(cat.id), 'subject': 's', 'description': 'd',
                      'priority': 'normal', 'site_id': ''})
    assert A.Ticket.query.one().team_id == team.id
