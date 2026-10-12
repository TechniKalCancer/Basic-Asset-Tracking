"""Pages: help desk reports (Tickets → Reports)."""
import csv
import io

from flask import Response, render_template, request

from foxdesk.core import app
from foxdesk.services.auth import _current_site_ids, require_permission
from foxdesk.services.helpdesk_reports import RANGES, build_helpdesk_report


def _range():
    key = request.args.get('range', '30')
    return key if key in dict(RANGES) else '30'


@app.route('/admin/helpdesk_reports')
@require_permission('tickets')
def admin_helpdesk_reports():
    key = _range()
    report = build_helpdesk_report(_current_site_ids(), key)
    return render_template('admin_helpdesk_reports.html', report=report, ranges=RANGES, range_key=key)


@app.route('/admin/helpdesk_reports/export')
@require_permission('tickets')
def admin_helpdesk_reports_export():
    key = _range()
    report = build_helpdesk_report(_current_site_ids(), key)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Ticket', 'Opened (UTC)', 'Subject', 'Category', 'School', 'Assigned to', 'Team', 'Priority', 'Status',
                'First reply (school hours)', 'Fix (school hours)', 'Reply on time', 'Fixed on time'])
    for r in report['rows']:
        t = r['ticket']
        w.writerow([t.id, f'{t.created_at:%Y-%m-%d %H:%M}', t.subject, r['category'], r['school'], r['tech'], r['team'],
                    t.priority, t.status, '' if r['reply'] is None else f'{r["reply"]:.2f}',
                    '' if r['fix'] is None else f'{r["fix"]:.2f}', r['reply_sla'] or '', r['fix_sla'] or ''])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="helpdesk-report-{key}.csv"'})
