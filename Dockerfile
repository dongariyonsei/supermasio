FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create static dirs (will be replaced by symlinks at runtime)
RUN mkdir -p static/qr static/uploads /app/data

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV ADMIN_USERNAME=admin
ENV ADMIN_PASSWORD=your-secure-password
ENV DATA_DIR=/app/data

# Expose the port the app runs on
EXPOSE 8000

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
