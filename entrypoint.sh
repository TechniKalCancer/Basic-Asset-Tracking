#!/bin/bash
set -e

# A fresh Docker named volume (e.g. branding-data) mounts owned by root
# regardless of the image's build-time ownership, so appuser cannot write to
# it until it is chown'd here. No-op once ownership is correct.
mkdir -p /app/instance/branding
chown -R appuser:appuser /app/instance/branding

# Everything past this point runs as appuser. The Dockerfile has no USER
# directive so this script can do the chown above first.
#
# Pending Alembic migrations are applied once here, before gunicorn forks its
# workers, so there is no multi-worker race. A database already at the latest
# revision makes this a no-op.
exec gosu appuser bash -c "flask db upgrade && exec gunicorn --bind 0.0.0.0:8081 --workers 3 --timeout 60 --access-logfile - app:app"
