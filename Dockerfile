# ─────────────────────────────────────────────────────────────
# Dockerfile — Nigeria Disease Surveillance API
# Base: Python 3.11 slim (smaller image, faster builds)
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Install system-level dependencies needed by
# geopandas (GDAL), psycopg2, and shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first — leverages Docker layer caching.
# Uses the slim API-only requirements (no ETL/notebook packages).
COPY requirements-api.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements-api.txt

# Copy the rest of the application source code
COPY src/ ./src/
COPY sql/ ./sql/

# Create a non-root user for security — running as root
# inside containers is a bad practice
RUN useradd --create-home appuser
USER appuser

# Expose the port FastAPI will listen on
EXPOSE 8000

# Health check — Docker will restart the container if this fails
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Start the FastAPI server
# $PORT is injected by Render.com — defaults to 8000 locally
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
