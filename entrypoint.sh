#!/bin/bash
set -e

# Applies any pending Alembic migrations before the app starts serving traffic.
# Safe to run on every boot: a database already at the latest revision is a
# no-op, and this runs once here rather than per-gunicorn-worker (unlike the
# old db.create_all() approach) so there's no multi-worker race to guard against.
flask db upgrade

exec gunicorn --bind 0.0.0.0:8081 --workers 3 --timeout 60 --access-logfile - app:app
