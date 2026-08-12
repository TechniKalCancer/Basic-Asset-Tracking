# Use the official Python image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies (gunicorn is already listed in requirements.txt)
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

ENV FLASK_APP=app.py

# gosu lets entrypoint.sh start as root just long enough to fix ownership on
# a freshly-created Docker volume (see entrypoint.sh), then drop to appuser
# before running any application code — same non-root end state as before,
# just with a root-capable step ahead of it now.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appuser /app \
    && chmod +x /app/entrypoint.sh

# Expose the port from app.py
EXPOSE 8081

# No curl/wget in python:3.9-slim, so the healthcheck hits /healthz with Python's stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8081/healthz', timeout=4).status == 200 else 1)"

# Container starts as root (no USER directive) so entrypoint.sh can fix
# volume ownership; it drops to appuser itself before running any app code —
# see entrypoint.sh. It then runs `flask db upgrade` once and execs gunicorn
# (3 workers, 60s timeout, access log to stdout).
ENTRYPOINT ["/app/entrypoint.sh"]
