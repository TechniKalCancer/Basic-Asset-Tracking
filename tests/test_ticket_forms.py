"""Request forms that change with the kind of issue, and help articles suggested while writing a ticket."""
from conftest import A


def category(client, name, questions=''):
    r = client.post('/admin/ticket_categories/new', data={'name': name, 'questions': questions}, follow_redirects=True)
    return A.TicketCategory.query.filter_by(name=name).first(), r


def test_category_questions_shape_the_request(client, make):
    screen, _ = category(client, 'Broken screen', "What's wrong with the screen?* | choice: Cracked, Lines, Black\n"
                                                  "Did it fall? | yes/no\nWhere were you? | text")
    assert [q['kind'] for q in screen.questions] == ['choice', 'yesno', 'text'] and screen.questions[0]['required']
    bad, r = category(client, 'Bad one', 'Color? | colour')
    assert bad is None and b"don&#39;t make sense" in r.data
    edit = client.get(f'/admin/ticket_categories/{screen.id}/edit').get_data(as_text=True)
    assert "What&#39;s wrong with the screen?* | choice: Cracked, Lines, Black" in edit

    page = client.get('/submit_ticket').get_data(as_text=True)
    assert 'stQuestionData' in page and 'Cracked' in page
    ana = make.person('Ana', 'Ruiz')
    base = {'person_id': ana.id, 'category_id': screen.id, 'subject': 'Screen', 'description': 'Help'}
    r = client.post('/submit_ticket', data=base, follow_redirects=True)
    assert b'Answer &#34;What&#39;s wrong with the screen?&#34;' in r.data and A.Ticket.query.count() == 0
    r = client.post('/submit_ticket', data=dict(base, q0='Lines', q1='Yes', q2=''), follow_redirects=True)
    ticket = A.Ticket.query.one()
    assert ticket.answers == [{'label': "What's wrong with the screen?", 'answer': 'Lines'}, {'label': 'Did it fall?', 'answer': 'Yes'}]
    detail = client.get(f'/admin/tickets/{ticket.id}').get_data(as_text=True)
    assert 'td-answers' in detail and 'Did it fall?' in detail
    client.post('/submit_ticket', data=dict(base, q0='Made up option'), follow_redirects=True)
    assert A.Ticket.query.count() == 1, 'an option that isn\'t offered counts as no answer'


def test_help_suggestions(client):
    wifi, _ = category(client, 'Wi-Fi')
    for title, body, cat in (('Chromebook won\'t connect to Wi-Fi', 'Forget the network and rejoin FCMS-Students.', None),
                             ('Printing from a Chromebook', 'Use Kyocera Cloud Print.', None),
                             ('Before you report slow internet', 'Restart the device first.', wifi.id)):
        client.post('/admin/help/new', data={'article_type': 'faq', 'title': title, 'body': body, 'is_active': 'on',
                                             'ticket_category_id': cat or ''})
    hits = client.get('/help/suggest?q=chromebook wont connect wifi').get_json()
    assert hits[0]['title'].startswith('Chromebook won') and hits[0]['url'].startswith('/help#help-')
    pinned = client.get(f'/help/suggest?q=&category_id={wifi.id}').get_json()
    assert [h['title'] for h in pinned] == ['Before you report slow internet'], 'pinned to the category'
    assert client.get('/help/suggest?q=zz').get_json() == []
    assert f'id="help-{hits[0]["id"]}"' in client.get('/help').get_data(as_text=True)
    assert A.HelpArticle.query.filter_by(ticket_category_id=wifi.id).count() == 1
