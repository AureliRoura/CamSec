#!/usr/bin/env python3
"""
CamSec Detector
===============
Subscribes to all camera MQTT streams, reassembles JPEG chunks,
runs YOLOv8 person detection and publishes alerts when a person is found.

Topics consumed (per camera prefix, e.g. "cam/01"):
  <prefix>/image/begin  – JSON {id, size, chunks, dark}
  <prefix>/image/data   – Binary [4B id BE][2B chunk_idx BE][JPEG data]
  <prefix>/image/end    – JSON {id, chunks, ok}

Topics published on detection:
  <prefix>/alert                – JSON summary (see AlertSummary below)
  <prefix>/alert/image/begin    – JSON {id, size, chunks, dark}
  <prefix>/alert/image/data     – Binary [4B id BE][2B chunk_idx BE][annotated JPEG]
  <prefix>/alert/image/end      – JSON {id, chunks, ok}

Global status:
  detector/status               – "online" | "offline" (retained)
"""

import os
import gc
import io
import json
import struct
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from ultralytics import YOLO

# ─── Configuration (environment variables) ───────────────────────────────────
MQTT_BROKER  = os.getenv("MQTT_BROKER",  "mosquitto")
MQTT_PORT    = int(os.getenv("MQTT_PORT",    "1883"))
MQTT_USER    = os.getenv("MQTT_USER",    "")
MQTT_PASS    = os.getenv("MQTT_PASS",    "")
MQTT_CLIENT  = os.getenv("MQTT_CLIENT",  "camsec-detector-" + time.strftime("%M%S"))
YOLO_MODEL   = os.getenv("YOLO_MODEL",   "yolov8n.pt")
CONFIDENCE   = float(os.getenv("CONFIDENCE",   "0.45"))
YOLO_IMGSZ   = int(os.getenv("YOLO_IMGSZ",   "640"))
CHUNK_SIZE   = int(os.getenv("CHUNK_SIZE",   "4096"))
STALE_TTL_S  = float(os.getenv("STALE_TTL_S",  "60.0"))
LOG_LEVEL    = os.getenv("LOG_LEVEL",    "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camsec.detector")


# ─── Image buffer ─────────────────────────────────────────────────────────────

@dataclass
class ImageBuffer:
    prefix:       str
    image_id:     int
    total_chunks: int
    size:         int
    dark:         bool
    ts:           float = field(default_factory=time.monotonic)
    chunks:       Dict[int, bytes] = field(default_factory=dict)

    def is_complete(self) -> bool:
        return len(self.chunks) == self.total_chunks

    def assemble(self) -> Optional[bytes]:
        if not self.is_complete():
            return None
        return b"".join(self.chunks[i] for i in range(self.total_chunks))


# Active image buffers keyed by (prefix, image_id)
_buffers: Dict[Tuple[str, int], ImageBuffer] = {}

# YOLO model and MQTT client (initialised in main())
_model:  Optional[YOLO]         = None
_client: Optional[mqtt.Client]  = None


# ─── Detection ────────────────────────────────────────────────────────────────

def _detect_persons(jpeg_bytes: bytes) -> Tuple[Optional[np.ndarray], List[dict]]:
    """
    Decode JPEG, run YOLO person detection, draw bounding boxes.

    Returns:
        (annotated_bgr_image, list_of_detections)
        On decode failure returns (None, []).
    """
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, []

    results   = _model(img, conf=CONFIDENCE, classes=[0], verbose=False, imgsz=YOLO_IMGSZ, device="cpu")
    detections: List[dict] = []

    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0])
            detections.append({
                "x1": x1, "y1": y1,
                "x2": x2, "y2": y2,
                "conf": round(conf, 3),
            })

            # Green bounding box
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), 2)
            label = f"person {conf:.0%}"
            (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            cv2.rectangle(img, (x1, y1 - lh - 6), (x1 + lw + 2, y1), (0, 220, 0), -1)
            cv2.putText(img, label, (x1 + 1, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

    # Timestamp watermark
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    cv2.putText(img, ts_str, (8, img.shape[0] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1,
                cv2.LINE_AA)

    return img, detections


# ─── Alert publishing ─────────────────────────────────────────────────────────

def _publish_chunks(topic_prefix: str, image_id: int, jpeg_bytes: bytes, dark: bool):
    """Publish a JPEG as chunked begin/data/end messages."""
    total_len  = len(jpeg_bytes)
    num_chunks = (total_len + CHUNK_SIZE - 1) // CHUNK_SIZE

    # BEGIN
    begin = json.dumps({
        "id": image_id, "size": total_len,
        "chunks": num_chunks, "dark": int(dark),
    })
    _client.publish(f"{topic_prefix}/begin", begin.encode(), retain=False)

    # DATA
    hdr = bytearray(6)
    hdr[0] = (image_id >> 24) & 0xFF
    hdr[1] = (image_id >> 16) & 0xFF
    hdr[2] = (image_id >>  8) & 0xFF
    hdr[3] =  image_id        & 0xFF

    for i in range(num_chunks):
        chunk  = jpeg_bytes[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        hdr[4] = (i >> 8) & 0xFF
        hdr[5] =  i       & 0xFF
        _client.publish(f"{topic_prefix}/data", bytes(hdr) + chunk, retain=False)

    # END
    end = json.dumps({"id": image_id, "chunks": num_chunks, "ok": 1})
    _client.publish(f"{topic_prefix}/end", end.encode(), retain=False)


def _publish_alert(buf: ImageBuffer, img: np.ndarray, detections: List[dict]):
    """Publish JSON summary + annotated JPEG for a person detection event."""
    ts  = time.time()
    prefix = buf.prefix

    log.info("[%s] ALERT image #%d – %d person(s) detected",
             prefix, buf.image_id, len(detections))

    # 1. JSON summary
    summary = {
        "camera":     prefix,
        "image_id":   buf.image_id,
        "ts":         round(ts, 3),
        "ts_iso":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "persons":    len(detections),
        "dark":       buf.dark,
        "detections": detections,
    }
    _client.publish(f"{prefix}/alert", json.dumps(summary).encode(), retain=False)

    # 2. Annotated image (chunked)
    ok, jpeg_buf = cv2.imencode(
        ".jpg", img,
        [cv2.IMWRITE_JPEG_QUALITY, 82],
    )
    if not ok:
        log.error("[%s] Failed to encode annotated image", prefix)
        return

    _publish_chunks(
        topic_prefix=f"{prefix}/alert/image",
        image_id=buf.image_id,
        jpeg_bytes=jpeg_buf.tobytes(),
        dark=buf.dark,
    )


# ─── Image processing ─────────────────────────────────────────────────────────

def _process_image(prefix: str, buf: ImageBuffer):
    """Assemble chunks and run person detection."""
    jpeg = buf.assemble()
    if jpeg is None:
        log.warning("[%s] Could not assemble image #%d", prefix, buf.image_id)
        return

    img, detections = _detect_persons(jpeg)
    if img is None:
        log.warning("[%s] Could not decode image #%d", prefix, buf.image_id)
        return

    log.debug("[%s] Image #%d – %d person(s)", prefix, buf.image_id, len(detections))

    if detections:
        _publish_alert(buf, img, detections)

    # Free memory explicitly (important on low-RAM devices like Raspberry Pi)
    del img, detections, jpeg
    gc.collect()


# ─── MQTT callbacks ───────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe("#", qos=0)
        client.publish("detector/status", b"online", retain=True)
    else:
        log.error("MQTT connection failed: rc=%d", rc)


def _on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning("Unexpected MQTT disconnect: rc=%d – will auto-reconnect", rc)


def _on_message(client, userdata, msg: mqtt.MQTTMessage):
    topic  = msg.topic
    parts  = topic.split("/")

    # We only care about topics ending in .../image/begin|data|end
    # Ignore our own alert topics to avoid loops
    if len(parts) < 3:
        return
    if "alert" in parts:
        return

    action = parts[-1]   # begin | data | end
    sub    = parts[-2]   # must be "image"
    if sub != "image" or action not in ("begin", "data", "end"):
        return

    prefix = "/".join(parts[:-2])  # e.g. "cam/01"

    try:
        if action == "begin":
            meta = json.loads(msg.payload)
            key  = (prefix, int(meta["id"]))
            _buffers[key] = ImageBuffer(
                prefix=prefix,
                image_id=int(meta["id"]),
                total_chunks=int(meta["chunks"]),
                size=int(meta["size"]),
                dark=bool(meta.get("dark", 0)),
            )
            log.debug("[%s] Image #%d started (%d chunks, %d B)",
                      prefix, meta["id"], meta["chunks"], meta["size"])

        elif action == "data":
            if len(msg.payload) < 6:
                return
            img_id = struct.unpack_from(">I", msg.payload, 0)[0]
            idx    = struct.unpack_from(">H", msg.payload, 4)[0]
            data   = bytes(msg.payload[6:])
            key    = (prefix, img_id)
            if key in _buffers:
                _buffers[key].chunks[idx] = data

        elif action == "end":
            meta = json.loads(msg.payload)
            key  = (prefix, int(meta["id"]))
            if not meta.get("ok", 0):
                _buffers.pop(key, None)
                return
            buf = _buffers.pop(key, None)
            if buf is None:
                return
            if buf.is_complete():
                _process_image(prefix, buf)
            else:
                log.warning("[%s] Incomplete image #%d: %d/%d chunks",
                            prefix, buf.image_id,
                            len(buf.chunks), buf.total_chunks)

    except Exception:
        log.exception("Error processing topic '%s'", topic)


# ─── Stale buffer cleanup ─────────────────────────────────────────────────────

def _purge_stale():
    now   = time.monotonic()
    stale = [k for k, b in _buffers.items()
             if now - b.ts > STALE_TTL_S]
    for k in stale:
        log.warning("Purging stale buffer %s", k)
        del _buffers[k]


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    global _model, _client

    log.info("Loading YOLO model: %s", YOLO_MODEL)
    _model = YOLO(YOLO_MODEL)
    _model.to("cpu")  # Force CPU – no GPU on Raspberry Pi
    log.info("Model ready (confidence threshold: %.0f%%)", CONFIDENCE * 100)

    _client = mqtt.Client(
        client_id=MQTT_CLIENT,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    if MQTT_USER:
        _client.username_pw_set(MQTT_USER, MQTT_PASS or None)

    _client.will_set("detector/status", b"offline", retain=True)
    _client.on_connect    = _on_connect
    _client.on_disconnect = _on_disconnect
    _client.on_message    = _on_message

    log.info("Connecting to %s:%d", MQTT_BROKER, MQTT_PORT)
    _client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    _client.loop_start()

    last_purge = time.monotonic()
    try:
        while True:
            time.sleep(5)
            if time.monotonic() - last_purge > 30:
                _purge_stale()
                last_purge = time.monotonic()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        _client.publish("detector/status", b"offline", retain=True)
        _client.loop_stop()
        _client.disconnect()


if __name__ == "__main__":
    main()
