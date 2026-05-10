# ─── CamSec Detector – Docker image ──────────────────────────────────────────
# Base: Python 3.11 slim (Debian Bookworm)
# Supports: amd64, arm64/v8, arm/v7
FROM python:3.11-slim

# Minimal system dependencies
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        libopenblas0 \
        liblapack3 \
        libgl1 \
        libglib2.0-0 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only PyTorch FIRST so ultralytics doesn't pull a CUDA build.
# The --index-url forces the CPU wheel on every platform (amd64 + arm64).
RUN pip install --no-cache-dir \
        torch \
        --index-url https://download.pytorch.org/whl/cpu

# Install remaining Python dependencies (layer cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY detector.py .

# Run with unbuffered output so logs appear immediately
CMD ["python", "-u", "detector.py"]
