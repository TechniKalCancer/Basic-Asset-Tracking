#!/bin/bash
set -e

# A fresh Docker named volume (e.g. branding-data, first boot after adding
# it) mounts owned by root regardless of what the image underneath was
# chown'd to at build time — appuser can't write into it until this fixes
# it. Cheap no-op on every later boot once ownership is already correct.
mkdir -p /app/instance/branding
chown -R appuser:appuser /app/instance/branding

# Everything past this point runs as appuser, not root — this container has
# no USER directive in the Dockerfile specifically so this script could do
# the chown above, then hand off immediately.
#
# Applies any pending Alembic migrations before the app starts serving traffic.
# Safe to run on every boot: a database already at the latest revision is a
# no-op, and this runs once here rather than per-gunicorn-worker (unlike the
# old db.create_all() approach) so there's no multi-worker race to guard against.
exec gosu appuser bash -c "flask db upgrade && exec gunicorn --bind 0.0.0.0:8081 --workers 3 --timeout 60 --access-logfile - app:app"
