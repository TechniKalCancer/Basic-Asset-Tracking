#!/bin/bash
# One-time setup for a new install: generates .env with strong random secrets
# from .env.example. Safe to re-run — it never touches an existing .env.
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then
  echo ".env already exists — leaving it alone."
  echo "Delete it first if you want setup.sh to generate a fresh one."
  exit 0
fi

if [ ! -f .env.example ]; then
  echo "Error: .env.example not found. Run this script from the repo root." >&2
  exit 1
fi

cp .env.example .env

SECRET_KEY=$(openssl rand -hex 32)
ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '=+/')
POSTGRES_PASSWORD=$(openssl rand -base64 18 | tr -d '=+/')

# GNU sed (Linux) needs `-i` with no argument; BSD/macOS sed needs `-i ''`.
sedi() {
  if sed --version >/dev/null 2>&1; then
    sed -i "$@"
  else
    sed -i '' "$@"
  fi
}

sedi "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" .env
sedi "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" .env
sedi "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD}|" .env

echo ""
echo "Generated .env with strong random secrets."
echo ""
echo "  Admin password: ${ADMIN_PASSWORD}"
echo ""
echo "Save that now — it will not be shown again. Log in at /admin/login with a"
echo "blank username and this password, then create your first site from the"
echo "dashboard once the app is running."
echo ""
echo "Next: docker compose -f docker-compose.deploy.yml up -d"
