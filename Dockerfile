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

# Run as a non-root user
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appuser /app
USER appuser

# Expose the port from app.py
EXPOSE 8081

# Production WSGI server: 3 workers, 60s timeout, access log to stdout
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "3", "--timeout", "60", "--access-logfile", "-", "app:app"]
