"""Photo attachments, ticket emails, guardian damage notices, email templates."""
import io

from conftest import A, png_bytes


def upload(file_bytes, name='photo.png'):
    return {'photos': [(io.BytesIO(file_bytes), name)]}


def test_incident_with_photo_and_guardian_notice(client, make, sent_emails):
    kid = make.person(guardian_name='Pat Parent', guardian_email='pat.parent@example.com')
    dev = make.device(holder=kid)
    r = client.post(f'/admin/assets/{dev.asset_tag}/incidents', content_type='multipart/form-data',
                    data={'description': 'Cracked screen', 'fee_amount': '45.00', 'notify_guardian': 'on',
                          **upload(png_bytes(), 'damage.png')})
    assert r.status_code == 302
    inc = A.Incident.query.one()
    att = A.Attachment.query.filter_by(owner_type='incident', owner_id=inc.id).one()
    assert att.content_type == 'image/png' and inc.guardian_notified_at is not None
    assert sent_emails[-1][0] == 'pat.parent@example.com' and '$45.00' in sent_emails[-1][2]

    r = client.get(f'/admin/attachments/{att.id}')
    assert r.data == png_bytes() and 'sandbox' in r.headers['Content-Security-Policy']

    client.post(f'/admin/incidents/{inc.id}/delete')
    assert A.Attachment.query.count() == 0, 'deleting an incident removes its photos'


def test_disguised_upload_is_rejected(client, make):
    dev = make.device(holder=make.person())
    client.post(f'/admin/assets/{dev.asset_tag}/incidents', data={'description': 'x'})
    inc = A.Incident.query.one()
    r = client.post(f'/admin/attachments/incident/{inc.id}', content_type='multipart/form-data',
                    data=upload(b'<html><script>alert(1)</script>', 'evil.png'), follow_redirects=True)
    assert A.Attachment.query.count() == 0 and b'supported photo' in r.data


def test_ticket_email_lifecycle(client, make, sent_emails):
    kid = make.person()
    cat = make.ticket_category()
    client.post('/admin/tickets/new', content_type='multipart/form-data',
                data={'person_id': str(kid.id), 'category_id': str(cat.id), 'subject': 'Wont charge',
                      'description': 'd', 'priority': 'normal', 'site_id': '', 'notify_requester': 'on'})
    t = A.Ticket.query.one()
    assert f'#{t.id}' in sent_emails[-1][1]

    n = len(sent_emails)
    client.post(f'/admin/tickets/{t.id}/comment', data={'body': 'internal'})
    assert len(sent_emails) == n, 'plain comments stay internal'
    client.post(f'/admin/tickets/{t.id}/comment', data={'body': 'Fixed it', 'email_requester': 'on'})
    assert 'Fixed it' in sent_emails[-1][2]
    assert [c.emailed_to_requester for c in A.TicketComment.query.order_by(A.TicketComment.id)] == [False, True]

    n = len(sent_emails)
    client.post(f'/admin/tickets/{t.id}/status', data={'status': 'resolved'})
    client.post(f'/admin/tickets/{t.id}/status', data={'status': 'closed'})
    assert len(sent_emails) == n + 1 and sent_emails[-1][1].startswith('Resolved:')


def test_ticket_emails_can_be_switched_off(client, make, sent_emails):
    client.post('/admin/emails', data={'action': 'notifications'})  # checkbox absent = off
    assert A._get_email_settings().ticket_notifications_enabled is False
    kid, cat = make.person(), make.ticket_category()
    client.post('/admin/tickets/new', data={'person_id': str(kid.id), 'category_id': str(cat.id), 'subject': 's',
                                            'description': 'd', 'priority': 'normal', 'site_id': '',
                                            'notify_requester': 'on'})
    assert sent_emails == []


def test_saving_one_email_card_keeps_the_others(client):
    client.post('/admin/emails', data={'action': 'save', 'loaner_overdue_subject': 'CUSTOM A', 'loaner_overdue_body': 'a'})
    client.post('/admin/emails', data={'action': 'save', 'ticket_reply_subject': 'CUSTOM B', 'ticket_reply_body': 'b'})
    s = A._get_email_settings()
    assert s.loaner_overdue_subject == 'CUSTOM A' and s.ticket_reply_subject == 'CUSTOM B'


def test_guardian_fields_and_csv_import(client, make):
    csv = ('first_name,last_name,email,role,parent_name,parent_email\n'
           'Csv,Kid,csv.kid@example.edu,student,Csv Parent,CSV.PARENT@example.com\n')
    client.post('/admin/people/import', content_type='multipart/form-data',
                data={'csv_file': (io.BytesIO(csv.encode()), 'people.csv')})
    p = A.Person.query.filter_by(email='csv.kid@example.edu').one()
    assert p.guardian_email == 'csv.parent@example.com' and p.guardian_name == 'Csv Parent'
