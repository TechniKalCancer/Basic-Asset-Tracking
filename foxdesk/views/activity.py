"""Pages: activity."""
from datetime import timedelta
from flask import render_template, request
from foxdesk.core import app, db
from foxdesk.models import ActivityLog
from foxdesk.services.util import _parse_date
from foxdesk.services.auth import _current_site_ids, require_permission
from foxdesk.services.scoping import _scope_activity_log
from foxdesk.web import ACTIVITY_LOG_ACTIONS


@app.route('/admin/activity')
@require_permission('admin')
def admin_activity():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('q', '').strip()
    action_filter = request.args.get('action', '').strip()
    since_str = request.args.get('since', '').strip()
    until_str = request.args.get('until', '').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    query = _scope_activity_log(ActivityLog.query, _current_site_ids())
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(ActivityLog.actor_label.ilike(like), ActivityLog.summary.ilike(like)))
    if action_filter in ACTIVITY_LOG_ACTIONS:
        query = query.filter(ActivityLog.action == action_filter)
    else:
        action_filter = ''
    since = _parse_date(since_str)
    if since:
        query = query.filter(ActivityLog.timestamp >= since)
    until = _parse_date(until_str)
    if until:
        query = query.filter(ActivityLog.timestamp < until + timedelta(days=1))
    query = query.order_by(ActivityLog.timestamp.asc() if sort_dir == 'asc' else ActivityLog.timestamp.desc())

    pagination = query.paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_activity.html', pagination=pagination, actions=ACTIVITY_LOG_ACTIONS,
                           search=search, action_filter=action_filter,
                           since=since_str, until=until_str, sort_dir=sort_dir)
