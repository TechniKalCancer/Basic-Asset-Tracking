"""Email to ticket: new mail, threaded replies, and what gets ignored. The mailbox is faked."""
from email.message import EmailMessage

from conftest import A, patch_everywhere, png_bytes
from foxdesk.services.secrets import encrypt_secret


def raw_email(frm, subject, body, message_id, in_reply_to=None, html=None, attach=None, **headers):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'], m['Message-ID'] = frm, 'helpdesk@district.org', subject, message_id
    if in_reply_to:
        m['In-Reply-To'] = m['References'] = in_reply_to
    for k, v in headers.items():
        m[k.replace('_', '-')] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype='html')
    if attach:
        m.add_attachment(attach[1], maintype='image', subtype='png', filename=attach[0])
    return bytes(m)


def mailbox(monkeypatch, *raws):
    row = A.mailbox_settings()
    row.kind, row.address, row.enabled = 'imap', 'helpdesk@district.org', True
    row.imap_host, row.imap_user = 'imap.example.org', 'helpdesk@district.org'
    row.imap_password = encrypt_secret('app-password', 'mailbox-secret')
    A.db.session.commit()
    messages = [A.parse_raw(r, uid=str(i)) for i, r in enumerate(raws)]
    patch_everywhere(monkeypatch, 'fetch_unread', lambda row: (None, messages))
    patch_everywhere(monkeypatch, 'mark_read', lambda row, handle, uid: None)
    return messages


def test_new_mail_becomes_a_ticket(make, monkeypatch, sent_emails):
    cat = make.ticket_category('Help desk')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org')
    mailbox(monkeypatch, raw_email('Ana Ruiz <Ana@Students.org>', 'My chromebook won\'t turn on',
                                   'It died in math class.', '<m1@students.org>', attach=('photo.png', png_bytes())))
    summary = A.check_mailbox()
    t = A.Ticket.query.one()
    assert summary.startswith('1 new ticket')
    assert (t.subject, t.requester_person_id, t.category_id) == ('My chromebook won\'t turn on', kid.id, cat.id)
    assert A.Attachment.query.filter_by(owner_type='ticket', owner_id=t.id).count() == 1
    to, subject, _ = sent_emails[-1]
    extra = sent_emails.extra[-1]
    assert to == 'ana@students.org' and f'[Ticket #{t.id}]' in subject
    assert extra['reply_to'] == 'helpdesk@district.org' and f'foxdesk-ticket-{t.id}-' in extra['headers']['Message-ID']
    assert A.InboundEmail.query.one().outcome == 'new'


def test_reply_threads_onto_its_ticket_and_reopens_it(make, monkeypatch, sent_emails):
    cat = make.ticket_category()
    t = A.Ticket(category_id=cat.id, subject='Charger', description='d', requester_email='ana@students.org',
                 requester_name='Ana', status='resolved')
    A.db.session.add(t)
    A.db.session.commit()
    body = 'Still broken, sorry!\n\nOn Tue, Oct 6, 2026 at 9:00 AM IT <it@district.org> wrote:\n> Fixed it\n'
    mailbox(monkeypatch,
            raw_email('Ana <ana@students.org>', 'Re: Update on your ticket', body, '<r1@students.org>',
                      in_reply_to=f'<foxdesk-ticket-{t.id}-abc123@district.org>'),
            raw_email('Stranger <who@else.org>', f'Re: Update on your ticket #{t.id}: Charger', 'let me in', '<r2@else.org>'))
    A.check_mailbox()
    t = A.Ticket.query.get(t.id)
    comment = A.TicketComment.query.filter_by(ticket_id=t.id).one()
    assert comment.body == 'Still broken, sorry!' and comment.from_requester and t.status == 'open'
    stranger = A.Ticket.query.filter(A.Ticket.id != t.id).one()
    assert stranger.requester_email == 'who@else.org', 'someone else replying starts their own ticket'


def test_ignored_and_duplicates(make, monkeypatch, sent_emails):
    make.ticket_category()
    patch_everywhere(monkeypatch, 'SMTP_FROM_EMAIL', 'noreply@district.org')
    mailbox(monkeypatch,
            raw_email('Ana <ana@students.org>', 'Automatic reply: away', 'Out until Monday', '<a1@x>', Auto_Submitted='auto-replied'),
            raw_email('List <news@vendor.com>', 'Newsletter', 'Deals!', '<a2@x>', Precedence='bulk'),
            raw_email('FoxDesk <noreply@district.org>', 'Resolved: thing', 'loop', '<a3@x>'),
            raw_email('Bo <bo@students.org>', 'Help', 'Please help', '<dup@x>'))
    A.check_mailbox()
    A.check_mailbox()
    outcomes = {e.message_id: e.outcome for e in A.InboundEmail.query}
    assert outcomes == {'<a1@x>': 'ignored', '<a2@x>': 'ignored', '<a3@x>': 'ignored', '<dup@x>': 'new'}
    assert A.Ticket.query.count() == 1, 'the same message is never handled twice'


def test_html_only_and_sender_rate_limit(make, monkeypatch, sent_emails):
    make.ticket_category()
    raws = [raw_email('Loop <loop@vendor.com>', f'Ticket {i}', 'x', f'<l{i}@x>') for i in range(11)]
    mailbox(monkeypatch, *raws)
    A.check_mailbox()
    assert A.Ticket.query.count() == 10 and A.InboundEmail.query.filter_by(outcome='ignored').count() == 1
    parsed = A.parse_raw(raw_email('X <x@y.org>', 'Hi', '', '<h@x>', html='<p>Hello<br>there</p><style>p{}</style>'))
    assert parsed['text'] == 'Hello\nthere'


def test_settings_page(client, make):
    make.ticket_category('Help desk')
    r = client.post('/admin/helpdesk_email', data={'address': 'helpdesk@district.org', 'kind': 'graph', 'enabled': 'on',
                                                   'graph_tenant': 'tenant-id'}, follow_redirects=True)
    assert b'left off' in r.data and not A.mailbox_settings().enabled
    client.post('/admin/helpdesk_email', data={'address': 'helpdesk@district.org', 'kind': 'graph', 'enabled': 'on',
                                               'graph_tenant': 'tenant-id', 'graph_client_id': 'client-id',
                                               'graph_client_secret': 'not-a-real-secret'})
    row = A.mailbox_settings()
    assert row.enabled and 'not-a-real-secret' not in row.graph_client_secret
    assert A.feature_enabled('email_tickets')
    page = client.get('/admin/helpdesk_email').get_data(as_text=True)
    assert 'not-a-real-secret' not in page and 'Saved. Leave blank to keep it' in page
