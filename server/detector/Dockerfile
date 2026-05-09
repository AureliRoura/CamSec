# ─── CamSec Detector – Docker image ──────────────────────────────────────────
# Base: Python 3.11 slim (Debian Bookworm)
FROM python:3.11-slim

# System dependencies required by OpenCV
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
 && rm -rf /var/lib/apt/lists/*

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
