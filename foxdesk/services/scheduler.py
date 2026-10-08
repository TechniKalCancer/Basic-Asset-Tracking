"""Background loops: loaner reminders and scheduled syncs."""
import threading
import time
from datetime import datetime
from foxdesk.core import EMAIL_ENABLED, GOOGLE_SYNC_ENABLED, KACE_SYNC_ENABLED, app, db, logger
from foxdesk.services.features import feature_enabled
from foxdesk.models import SyncSchedule
from foxdesk.services.auth import _log_activity
from foxdesk.integrations.google import _run_google_device_sync, _run_google_people_sync
from foxdesk.integrations.kace import _run_kace_device_sync
from foxdesk.services.assignments import _send_overdue_loaner_reminders


SYNC_SCHEDULE_INTERVALS = {
    1: 'Every hour', 3: 'Every 3 hours', 6: 'Every 6 hours', 12: 'Every 12 hours',
    24: 'Once a day', 168: 'Once a week',
}


def _get_or_create_sync_schedule(sync_type):
    schedule = SyncSchedule.query.filter_by(sync_type=sync_type).first()
    if not schedule:
        schedule = SyncSchedule(sync_type=sync_type, enabled=False, interval_hours=24)
        db.session.add(schedule)
        db.session.commit()
    return schedule


def _loaner_reminder_loop():
    """Background daemon: checks for overdue loaners once an hour so students
    get emailed automatically without anyone having to click a button. The
    reminder_sent_at gate in _send_overdue_loaner_reminders() keeps this safe
    even though gunicorn runs multiple worker processes, each with their own
    copy of this loop."""
    while True:
        time.sleep(3600)
        try:
            with app.app_context():
                if feature_enabled('loaners') and feature_enabled('reminders'):
                    _send_overdue_loaner_reminders()
        except Exception as e:
            logger.error('Loaner reminder background loop error: %s', e)


if EMAIL_ENABLED:
    threading.Thread(target=_loaner_reminder_loop, daemon=True).start()


def _run_due_scheduled_syncs():
    """Runs any enabled SyncSchedule whose interval has elapsed. Claims a
    schedule (stamps last_run_at and commits) BEFORE doing the actual sync
    work, narrowing the window where two gunicorn workers both see it as
    due at once — same accepted-risk idempotency approach as the loaner
    reminder loop, just applied via a timestamp column instead of a
    per-row resend gate."""
    google_on, kace_on = feature_enabled('google'), feature_enabled('kace')  # configured AND switched on
    if not google_on and not kace_on:
        return
    now = datetime.utcnow()
    for schedule in SyncSchedule.query.filter_by(enabled=True).all():
        if schedule.sync_type in ('person', 'device') and not google_on:
            continue
        if schedule.sync_type == 'kace' and not kace_on:
            continue
        if schedule.last_run_at and (now - schedule.last_run_at).total_seconds() < schedule.interval_hours * 3600:
            continue
        schedule.last_run_at = now
        db.session.commit()
        try:
            if schedule.sync_type == 'person':
                matched, updated, unmatched, created = _run_google_people_sync()
                schedule.last_run_summary = f'{matched} matched, {updated} updated, {created} auto-created, {unmatched} unmatched'
            elif schedule.sync_type == 'device':
                # No deadline — this runs in the background thread, not an
                # HTTP request, so it can take as long as a full backfill needs.
                matched, updated, unmatched, pushed, _truncated = _run_google_device_sync()
                schedule.last_run_summary = f'{matched} matched, {updated} updated, {pushed} pushed, {unmatched} unmatched'
            else:
                matched, updated, unmatched, created = _run_kace_device_sync()
                schedule.last_run_summary = f'{matched} matched, {updated} updated, {created} auto-created, {unmatched} unmatched'
            _log_activity('scheduled_sync', f'Scheduled {schedule.sync_type} sync ran: {schedule.last_run_summary}')
        except Exception as e:
            schedule.last_run_summary = f'Failed: {e}'
            logger.error('Scheduled %s sync failed: %s', schedule.sync_type, e)
        db.session.commit()


def _scheduled_sync_loop():
    """Background daemon: checks every 15 minutes whether a People or
    Device sync is due per its SyncSchedule (see /admin/sync_schedule),
    and runs it if so. Same multi-worker-safe pattern as
    _loaner_reminder_loop above."""
    while True:
        time.sleep(900)
        try:
            with app.app_context():
                _run_due_scheduled_syncs()
        except Exception as e:
            logger.error('Scheduled sync background loop error: %s', e)


# Either integration needs the loop — this used to start only for Google,
# so a KACE-only install never ran its scheduled syncs.
if GOOGLE_SYNC_ENABLED or KACE_SYNC_ENABLED:
    threading.Thread(target=_scheduled_sync_loop, daemon=True).start()
