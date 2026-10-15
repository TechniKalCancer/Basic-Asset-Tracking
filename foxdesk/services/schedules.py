"""Student class schedules from the SIS, and where a student is right now.

The import takes a CSV with one row per class per student. Column names vary
by SIS, so each field accepts the usual spellings (PowerSchool's
Student_Number / Expression / Course_Name, or plain Period / Course / Teacher
/ Room), with any "Table." or "[05]" prefix ignored. A student is found by
student number (Person.external_id), then email, then exact name. Every
student the file matches gets their whole schedule replaced; students not in
the file keep theirs, so one grade's file at a time works.

Where-now needs the school's bell times (Site.bell_schedule, one period per
line: "1, 7:55, 8:48"). Periods match the schedule by their number when they
have one — "1(A)", "1st" and "Period 1" are all period 1 — else by name.
"""
import csv
import io
import re
from collections import defaultdict
from datetime import datetime, time

from foxdesk.clock import utcnow
from foxdesk.core import db
from foxdesk.models import Person, PersonIdentity, StudentSchedule

FIELDS = {
    'student_number': ('student number', 'student id', 'studentid', 'student no', 'stu id', 'sis id', 'local id',
                       'external id', 'id'),
    'email': ('email', 'student email', 'email address', 'e mail'),
    'first_name': ('first name', 'firstname', 'student first name', 'first'),
    'last_name': ('last name', 'lastname', 'student last name', 'last'),
    'name': ('name', 'student name', 'lastfirst', 'full name', 'student'),
    'period': ('period', 'expression', 'per', 'hour', 'block', 'period block', 'class period'),
    'course': ('course', 'course name', 'class', 'class name', 'course title', 'section', 'subject'),
    'teacher': ('teacher', 'teacher name', 'instructor', 'teachers', 'lastfirst teacher', 'teacher lastfirst'),
    'room': ('room', 'room number', 'classroom', 'rm', 'location'),
    'term': ('term', 'termid', 'term name', 'semester', 'term abbreviation'),
}
MAX_ROWS = 50000


def _norm_header(h):
    h = re.sub(r'^\[\d+\]', '', (h or '').strip())
    h = h.rsplit('.', 1)[-1]
    return re.sub(r'\s+', ' ', re.sub(r'[_\-/]+', ' ', h)).strip().lower()


def map_columns(headers):
    """{field: column index} for the columns this file has."""
    found = {}
    normed = [_norm_header(h) for h in headers]
    for field, names in FIELDS.items():
        for name in names:
            if name in normed:
                found[field] = normed.index(name)
                break
    return found


def _clean(v, n):
    v = (v or '').strip()
    return v[:n] if v else None


def _people_index():
    by_number, by_email, by_name = {}, {}, defaultdict(list)
    for p in Person.query.filter_by(role='student'):
        if p.external_id:
            by_number[p.external_id.strip().lstrip('0') or '0'] = p
        if p.email:
            by_email[p.email.strip().lower()] = p
        by_name[(p.first_name.strip().lower(), p.last_name.strip().lower())].append(p)
    for ident in PersonIdentity.query.filter(PersonIdentity.person_id.isnot(None), PersonIdentity.email.isnot(None)):
        by_email.setdefault(ident.email.strip().lower(), ident.person)
    return by_number, by_email, by_name


def _split_name(value):
    """'Ruiz, Ana' or 'Ana Ruiz' → (first, last)."""
    value = (value or '').strip()
    if ',' in value:
        last, first = [x.strip() for x in value.split(',', 1)]
        return first.split(' ')[0] if first else '', last
    parts = value.split()
    return (parts[0], ' '.join(parts[1:])) if len(parts) > 1 else ('', value)


def _find(row, cols, index):
    by_number, by_email, by_name = index

    def get(field):
        i = cols.get(field)
        return (row[i] if i is not None and i < len(row) else '').strip()
    number, email = get('student_number'), get('email').lower()
    if number and (number.lstrip('0') or '0') in by_number:
        return by_number[number.lstrip('0') or '0'], None
    if email and email in by_email:
        return by_email[email], None
    first, last = get('first_name'), get('last_name')
    if not (first and last) and get('name'):
        first, last = _split_name(get('name'))
    if first and last:
        hits = by_name.get((first.lower(), last.lower()), [])
        active = [p for p in hits if p.is_active]
        hits = active if len(active) == 1 else hits
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, f'{len(hits)} students named {first} {last}; add a student number column'
    who = number or email or f'{first} {last}'.strip() or '(no student)'
    return None, f'no student matches {who}'


def import_schedules(file_bytes):
    """Reads the CSV and replaces the schedule of every student it matches.
    Returns a summary dict; on a file it can't use, summary['error']."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',\t;')
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return dict(error='That file is empty.')
    cols = map_columns(rows[0])
    who = {'student_number', 'email', 'name'} & cols.keys() or ({'first_name', 'last_name'} <= cols.keys())
    if 'period' not in cols or not who:
        missing = ([] if 'period' in cols else ['a period column (Period, Expression, Block…)']) + \
                  ([] if who else ['a student column (Student Number, Email, or First and Last Name)'])
        return dict(error='Missing ' + ' and '.join(missing) + '. Columns found: ' + ', '.join(rows[0][:15]))
    if len(rows) - 1 > MAX_ROWS:
        return dict(error=f'That file has {len(rows) - 1:,} rows; the limit is {MAX_ROWS:,}. Split it by grade.')

    index = _people_index()
    per_person, unmatched = defaultdict(list), []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        person, why = _find(row, cols, index)
        period = _clean(row[cols['period']] if cols['period'] < len(row) else '', 40)
        if not period:
            why = why or 'no period'
        if person is None or why:
            unmatched.append((line_no, why, ', '.join(c for c in row[:6] if c.strip())))
            continue

        def get(field, n):
            i = cols.get(field)
            return _clean(row[i] if i is not None and i < len(row) else '', n)
        per_person[person.id].append(dict(period=period, course=get('course', 160), teacher=get('teacher', 120),
                                          room=get('room', 40), term=get('term', 40)))

    now = utcnow()
    if per_person:
        StudentSchedule.query.filter(StudentSchedule.person_id.in_(per_person)).delete(synchronize_session=False)
        for pid, classes in per_person.items():
            seen = set()
            for c in classes:
                key = (c['period'], c['course'], c['teacher'], c['room'], c['term'])
                if key not in seen:
                    seen.add(key)
                    db.session.add(StudentSchedule(person_id=pid, imported_at=now, **c))
    return dict(students=len(per_person), classes=sum(len(v) for v in per_person.values()), unmatched=unmatched,
                columns={f: rows[0][i] for f, i in cols.items()})


# ─── bell times and where-now ────────────────────────────────────────────────

_BELL_LINE = re.compile(r'^\s*(?P<label>.+?)[\s,;|]+(?P<start>\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?)\s*(?:-|–|to|,|;|\s)\s*'
                        r'(?P<end>\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?)\s*$', re.I)


def _parse_time(value):
    m = re.match(r'(\d{1,2}):(\d{2})\s*([ap])?', value.strip().lower())
    hour, minute, half = int(m.group(1)), int(m.group(2)), m.group(3)
    if half == 'p' and hour < 12:
        hour += 12
    elif half == 'a' and hour == 12:
        hour = 0
    elif not half and 1 <= hour <= 5:
        hour += 12  # a school day: "1:30" without am/pm is the afternoon
    return time(hour, minute)


def parse_bell_schedule(text):
    """([dict(label, key, start, end)], [problem lines]) in the order written."""
    periods, problems = [], []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = _BELL_LINE.match(line)
        if not m:
            problems.append(line)
            continue
        label = m.group('label').strip().rstrip(',;:')
        start, end = _parse_time(m.group('start')), _parse_time(m.group('end'))
        if end <= start:
            problems.append(line)
            continue
        periods.append(dict(label=label, key=period_key(label), start=start, end=end))
    return periods, problems


_NAME_ALIASES = {'homeroom': 'hr', 'home room': 'hr', 'advisory': 'adv', 'lunch': 'lunch'}


def period_key(label):
    """'1(A)', '1st', 'Period 1' → '1'; 'Homeroom' → 'hr'."""
    label = (label or '').strip().lower()
    m = re.search(r'\d+', label)
    if m:
        return str(int(m.group()))
    label = re.sub(r'[^a-z ]', '', label).strip()
    return _NAME_ALIASES.get(label, label.replace(' ', ''))


def schedule_rows(person):
    """The person's classes in the order of their school's bell times (so
    homeroom comes first), else by period number, then anything else."""
    bells, _ = parse_bell_schedule(person.site.bell_schedule if person.site else '')
    position = {}
    for i, p in enumerate(bells):
        position.setdefault(p['key'], i)

    def order(c):
        k = period_key(c.period)
        if k in position:
            return (0, position[k], 0, c.period)
        return (1, 0, int(k), c.period) if k.isdigit() else (2, 0, 0, c.period)
    return sorted(person.schedule, key=order)


def where_now(person, now=None):
    """dict(state, ...) for the person page:
    in_class   - bell (the period now) and classes (theirs that period);
    passing    - between periods: next is the coming period;
    out        - before or after the school day, or not a school day;
    no_bell    - their school has no bell times (or they have no school)."""
    from foxdesk.services.sla import Calendar, sla_settings
    if not person.schedule:
        return None
    if not person.site or not person.site.bell_schedule:
        return dict(state='no_bell')
    periods, _ = parse_bell_schedule(person.site.bell_schedule)
    if not periods:
        return dict(state='no_bell')
    cal = Calendar(sla_settings())
    local = cal.local(now or utcnow())
    if not cal.is_school_day(local.date()):
        return dict(state='out', when=local, reason='not a school day')
    clock = local.time().replace(second=0, microsecond=0)
    for p in periods:
        if p['start'] <= clock < p['end']:
            classes = [c for c in person.schedule if period_key(c.period) == p['key']]
            return dict(state='in_class', when=local, bell=p, classes=classes)
    upcoming = [p for p in periods if p['start'] > clock]
    if upcoming and clock >= periods[0]['start']:
        return dict(state='passing', when=local, next=upcoming[0],
                    classes=[c for c in person.schedule if period_key(c.period) == upcoming[0]['key']])
    return dict(state='out', when=local, reason='before school' if upcoming else 'after school')


def fmt_time(t):
    return datetime.combine(datetime.min, t).strftime('%-I:%M %p')
