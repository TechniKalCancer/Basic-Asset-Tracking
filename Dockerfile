# Use the official Python image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies (Added gunicorn for better production performance)
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy the rest of the application
COPY . .

# Expose the port from app.py
EXPOSE 8081

# Run the app using gunicorn (Production ready)
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "app:app"]