FROM python:3.12-slim

WORKDIR /app

# Requirements are copied alone first so the dependency layer stays cached
# until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_APP=app.py

# gosu lets entrypoint.sh start as root just long enough to fix ownership on
# a freshly-created Docker volume (see entrypoint.sh), then drop to appuser
# before running any application code.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appuser /app \
    && chmod +x /app/entrypoint.sh

EXPOSE 8081

# No curl/wget in python:3.12-slim, so the healthcheck hits /healthz with Python's stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8081/healthz', timeout=4).status == 200 else 1)"

# Container starts as root (no USER directive) so entrypoint.sh can fix
# volume ownership; it drops to appuser itself before running any app code —
# see entrypoint.sh. It then runs `flask db upgrade` once and execs gunicorn
# (3 workers, 60s timeout, access log to stdout).
ENTRYPOINT ["/app/entrypoint.sh"]
