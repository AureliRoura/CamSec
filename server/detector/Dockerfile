# ─── CamSec Detector – Docker image ──────────────────────────────────────────
# Base: Python 3.11 slim (Debian Bookworm)
# Supports: amd64, arm64/v8, arm/v7
FROM python:3.11-slim

# Minimal system dependencies (opencv-python-headless includes its own libs)
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        libopenblas0 \
        liblapack3 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download YOLOv8 nano weights at build time so the container starts
# immediately without needing internet access at runtime.
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# Copy application code
COPY detector.py .

# Run with unbuffered output so logs appear immediately
CMD ["python", "-u", "detector.py"]
