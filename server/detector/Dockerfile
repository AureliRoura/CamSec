# ─── CamSec Detector – Docker image ──────────────────────────────────────────
# Base: Python 3.11 slim (Debian Bookworm)
# Supports: amd64, arm64/v8, arm/v7
FROM python:3.11-slim

# System dependencies required by OpenCV
# Install build tools first to resolve dependencies properly on ARM
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libatlas-base-dev \
        libjasper-dev \
        libtiff5 \
        libjasper1 \
        libharfbuzz0b \
        libwebp6 \
        libtiff5 \
        libopenjp2-7 \
        libjasper1 \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get clean

WORKDIR /app

# Install Python dependencies first (layer cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download YOLOv8 nano weights at build time so the container starts
# immediately without needing internet access at runtime.
# The model is stored in /root/.config/Ultralytics/
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# Copy application code
COPY detector.py .

# Run with unbuffered output so logs appear immediately
CMD ["python", "-u", "detector.py"]
