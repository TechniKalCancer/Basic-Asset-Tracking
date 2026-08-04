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

# Run as a non-root user
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appuser /app \
    && chmod +x /app/entrypoint.sh
USER appuser

# Expose the port from app.py
EXPOSE 8081

# entrypoint.sh runs `flask db upgrade` once, then execs gunicorn (3 workers, 60s timeout,
# access log to stdout) — see entrypoint.sh for why the migration runs there and not per-worker.
ENTRYPOINT ["/app/entrypoint.sh"]
