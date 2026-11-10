"""Request forms that change with the kind of issue, and help articles
suggested while someone writes a ticket.

A ticket category's questions are edited as one line each:
    Which room? | text
    Is it charging? | yes/no
    What's wrong with the screen?* | choice: Cracked, Lines, Black
A trailing * on the question makes it required; a line with no "|" is a
text question. The answers are kept on the ticket (Ticket.answers).
"""
import re

from foxdesk.core import db
from foxdesk.models import HelpArticle

KINDS = ('text', 'choice', 'yesno')
MAX_QUESTIONS = 12


def parse_questions(text):
    """(questions, problems) from the editor text."""
    questions, problems = [], []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        label, _, spec = (part.strip() for part in line.partition('|'))
        required = label.endswith('*')
        label = label.rstrip('*').strip()[:160]
        kind, _, rest = (part.strip() for part in spec.partition(':'))
        kind = kind.lower().replace(' ', '')
        q = dict(label=label, kind='text', options=[], required=required)
        if not label:
            problems.append(line)
            continue
        if kind == 'choice':
            q['options'] = [o.strip()[:80] for o in rest.split(',') if o.strip()]
            if len(q['options']) < 2:
                problems.append(line)
                continue
            q['kind'] = 'choice'
        elif kind in ('yes/no', 'yesno', 'y/n'):
            q['kind'] = 'yesno'
        elif kind not in ('', 'text'):
            problems.append(line)
            continue
        questions.append(q)
    if len(questions) > MAX_QUESTIONS:
        problems.append(f'at most {MAX_QUESTIONS} questions')
    return questions[:MAX_QUESTIONS], problems


def format_questions(questions):
    lines = []
    for q in questions or []:
        label = q['label'] + ('*' if q.get('required') else '')
        if q['kind'] == 'choice':
            lines.append(f'{label} | choice: {", ".join(q["options"])}')
        elif q['kind'] == 'yesno':
            lines.append(f'{label} | yes/no')
        else:
            lines.append(f'{label} | text')
    return '\n'.join(lines)


def read_answers(category, form):
    """([{label, answer}], error) for this category's questions from the
    submitted form (fields q0, q1, ...)."""
    answers = []
    for i, q in enumerate((category.questions or []) if category else []):
        value = (form.get(f'q{i}') or '').strip()[:500]
        if q['kind'] == 'choice' and value and value not in q['options']:
            value = ''
        if q['kind'] == 'yesno' and value not in ('Yes', 'No'):
            value = ''
        if q.get('required') and not value:
            return None, f'Answer "{q["label"]}".'
        if value:
            answers.append(dict(label=q['label'], answer=value))
    return answers, None


# ─── help suggestions ─────────────────────────────────────────────────────────

_STOP = {'the', 'and', 'for', 'with', 'not', 'my', 'is', 'it', 'on', 'in', 'to', 'of', 'a', 'an', 'can', 'cant', "can't",
         'wont', "won't", 'does', 'doesnt', "doesn't", 'help', 'please', 'have', 'has', 'this', 'that', 'from', 'when'}


def suggest_articles(text, category_id=None, limit=3):
    """Active help articles that look relevant to what's been typed so far,
    plus any pinned to the chosen category, best first."""
    words = [w for w in re.findall(r"[a-z0-9']+", (text or '').lower()) if len(w) >= 3 and w not in _STOP][:8]
    query = HelpArticle.query.filter(HelpArticle.is_active.is_(True))
    if not words and not category_id:
        return []
    conditions = [HelpArticle.title.ilike(f'%{w}%') for w in words] + [HelpArticle.body.ilike(f'%{w}%') for w in words]
    if category_id:
        conditions.append(HelpArticle.ticket_category_id == category_id)
    scored = []
    for a in query.filter(db.or_(*conditions)).limit(60):
        title, body = a.title.lower(), a.body.lower()
        score = sum(3 for w in words if w in title) + sum(1 for w in words if w in body)
        if category_id and a.ticket_category_id == category_id:
            score += 4
        if score:
            scored.append((score, a))
    scored.sort(key=lambda x: (-x[0], x[1].title))
    return [a for _, a in scored[:limit]]
