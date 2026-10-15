"""Student schedules from the SIS: column mapping, matching students, replacing
schedules, bell times, and where a student is right now."""
import io
from datetime import datetime, time

from conftest import A
from foxdesk.services.schedules import import_schedules, map_columns, parse_bell_schedule, period_key, where_now

BELLS = 'HR, 7:45, 7:55\n1st, 7:55 - 8:48\n2, 8:52, 9:45\n3, 9:49, 10:42\nLunch 11:30-12:00\n7, 2:10, 3:05'


def csv_bytes(*lines):
    return ('\n'.join(lines) + '\n').encode()


def test_column_names_from_different_sis_exports():
    cols = map_columns(['Students.Student_Number', '[13]Expression', 'Course_Name', 'Teacher', 'Room', 'TermID'])
    assert cols == dict(student_number=0, period=1, course=2, teacher=3, room=4, term=5)
    cols = map_columns(['First Name', 'Last Name', 'Period', 'Class', 'Instructor'])
    assert {'first_name', 'last_name', 'period', 'course', 'teacher'} == set(cols)


def test_import_matches_and_replaces(make):
    site = make.site('Middle')
    ana = make.person('Ana', 'Ruiz', external_id='100234', site=site)
    make.person('Bo', 'Lee', email='bo@students.org')
    make.person('Cy', 'Day')
    twin1, twin2 = make.person('Sam', 'Twin'), make.person('Sam', 'Twin')
    keep = make.person('Kim', 'Keep')
    A.db.session.add(A.StudentSchedule(person_id=keep.id, period='1', course='Old'))
    A.db.session.add(A.StudentSchedule(person_id=ana.id, period='9', course='Last year'))
    A.db.session.commit()

    result = import_schedules(csv_bytes(
        'Student_Number,Email,Name,Expression,Course_Name,Teacher,Room',
        '0100234,,,1(A),Math 7,Smith,112',            # leading zeros still match
        '100234,,,2(A),Science 7,Albion,204',
        ',bo@students.org,,1(A),Art,Jones,ART',
        ',,"Day, Cy",HR,Homeroom,Day,101',
        ',,Sam Twin,1(A),PE,Coach,GYM',
        '999,,,1(A),Ghost,Nobody,0',
        '100234,,,,No period,X,1',
    ))
    assert (result['students'], result['classes']) == (3, 4)
    reasons = {line: why for line, why, _ in result['unmatched']}
    assert '2 students named Sam Twin' in reasons[6] and 'no student matches 999' in reasons[7] and reasons[8] == 'no period'
    A.db.session.commit()
    assert sorted((c.period, c.course) for c in A.db.session.get(A.Person, ana.id).schedule) == [('1(A)', 'Math 7'), ('2(A)', 'Science 7')], \
        'last year\'s schedule is replaced'
    assert [c.course for c in A.db.session.get(A.Person, keep.id).schedule] == ['Old'], 'not in the file: kept'
    assert A.StudentSchedule.query.filter(A.StudentSchedule.person_id.in_([twin1.id, twin2.id])).count() == 0

    assert 'Missing a period column' in import_schedules(csv_bytes('Student_Number,Course', '1,Math'))['error']
    assert 'student column' in import_schedules(csv_bytes('Period,Course', '1,Math'))['error']


def test_bell_times_and_period_keys():
    periods, problems = parse_bell_schedule(BELLS + '\nbroken line\n8, 3:10, 3:00')
    assert [p['key'] for p in periods] == ['hr', '1', '2', '3', 'lunch', '7']
    assert periods[-1]['start'] == time(14, 10), 'afternoon without pm'
    assert problems == ['broken line', '8, 3:10, 3:00']
    assert [period_key(x) for x in ('1(A)', '1st', 'Period 1', 'Homeroom', 'HR')] == ['1', '1', '1', 'hr', 'hr']


def test_where_now(make):
    site = make.site('Middle')
    site.bell_schedule = BELLS
    ana = make.person('Ana', 'Ruiz', site=site)
    A.db.session.add_all([A.StudentSchedule(person_id=ana.id, period='1(A)', course='Math 7', teacher='Smith', room='112'),
                          A.StudentSchedule(person_id=ana.id, period='2(A)', course='Science 7', teacher='Albion', room='204')])
    A.db.session.commit()
    ana = A.db.session.get(A.Person, ana.id)
    at = lambda h, m: datetime(2026, 10, 14, h + 4, m)  # Wednesday, New York (UTC-4)
    now = where_now(ana, at(8, 10))
    assert now['state'] == 'in_class' and now['bell']['label'] == '1st' and now['classes'][0].room == '112'
    passing = where_now(ana, at(8, 50))
    assert passing['state'] == 'passing' and passing['next']['label'] == '2' and passing['classes'][0].course == 'Science 7'
    assert where_now(ana, at(16, 0))['reason'] == 'after school'
    assert where_now(ana, datetime(2026, 10, 17, 14, 0))['reason'] == 'not a school day'
    A.db.session.add(A.StudentSchedule(person_id=ana.id, period='HR', course='Homeroom'))
    A.db.session.commit()
    from foxdesk.services.schedules import schedule_rows
    assert [c.period for c in schedule_rows(A.db.session.get(A.Person, ana.id))] == ['HR', '1(A)', '2(A)'], 'bell order'
    site.bell_schedule = None
    A.db.session.commit()
    assert where_now(ana)['state'] == 'no_bell'
    assert where_now(make.person('No', 'Schedule')) is None


def test_import_page_and_person_page(client, make):
    site = make.site('Middle')
    site.bell_schedule = BELLS
    ana = make.person('Ana', 'Ruiz', external_id='100234', site=site)
    A.db.session.commit()
    data = {'file': (io.BytesIO(csv_bytes('Student_Number,Period,Course,Teacher,Room', '100234,1,Math 7,Smith,112')), 'sched.csv')}
    r = client.post('/admin/people/schedules', data=data, content_type='multipart/form-data', follow_redirects=True)
    page = r.get_data(as_text=True)
    assert 'Imported 1 classes for 1 students' in page and 'student number = “Student_Number”' in page
    person = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Schedule <span' in person and 'Math 7' in person and 'Smith' in person
    assert ('Now' in person) or ('Between classes' in person) or ('Not in class now' in person)
    bad = {'file': (io.BytesIO(b'x'), 'sched.xlsx')}
    assert b'Save the export as CSV' in client.post('/admin/people/schedules', data=bad, content_type='multipart/form-data',
                                                     follow_redirects=True).data
    assert 'Bell times' in client.get('/admin/people/schedules').get_data(as_text=True)
