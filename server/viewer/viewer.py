#!/usr/bin/env python3
"""
CamSec Viewer
=============
Lightweight HTTP server that subscribes to MQTT alert topics, reassembles
annotated JPEG images and serves them via a real-time web dashboard.

Endpoints:
  GET /                   – Dashboard (auto-updating, iframe-embeddable)
  GET /embed              – Minimal panel for embedding in an existing page
  GET /stream             – Server-Sent Events stream (real-time alerts)
  GET /api/alerts         – JSON list of recent alerts (all cameras)
  GET /api/alerts/latest  – JSON: one latest alert per camera
  GET /api/image/<img_id> – Serve annotated JPEG by id
  GET /help               – This help: available endpoints and config variables
"""

import base64
import io
import json
import logging
import os
import queue
import struct
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from flask import Flask, Response, jsonify, render_template_string, send_file, abort
import paho.mqtt.client as mqtt

# ─── Configuration ────────────────────────────────────────────────────────────
MQTT_BROKER  = os.getenv("MQTT_BROKER",  "localhost")
MQTT_PORT    = int(os.getenv("MQTT_PORT",    "1883"))
MQTT_USER    = os.getenv("MQTT_USER",    "")
MQTT_PASS    = os.getenv("MQTT_PASS",    "")
MQTT_CLIENT  = os.getenv("MQTT_CLIENT",  "camsec-viewer")
CHUNK_SIZE   = int(os.getenv("CHUNK_SIZE",   "4096"))
MAX_ALERTS   = int(os.getenv("MAX_ALERTS",   "50"))   # alerts kept in memory
HTTP_HOST    = os.getenv("HTTP_HOST",    "0.0.0.0")
HTTP_PORT    = int(os.getenv("HTTP_PORT",    "8080"))
LOG_LEVEL    = os.getenv("LOG_LEVEL",    "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camsec.viewer")

# ─── Alert store ──────────────────────────────────────────────────────────────

@dataclass
class Alert:
    img_id:    int
    camera:    str
    ts_iso:    str
    ts:        float
    persons:   int
    dark:      bool
    jpeg:      bytes        # annotated JPEG
    b64:       str = ""     # base64 for JSON API (filled lazily)

    def to_dict(self, include_image: bool = False) -> dict:
        d = {
            "img_id":  self.img_id,
            "camera":  self.camera,
            "ts_iso":  self.ts_iso,
            "ts":      self.ts,
            "persons": self.persons,
            "dark":    self.dark,
            "url":     f"/api/image/{self.img_id}",
        }
        if include_image:
            if not self.b64:
                self.b64 = base64.b64encode(self.jpeg).decode()
            d["image_b64"] = self.b64
        return d


# Ring-buffer of recent alerts (thread-safe via lock)
_lock          = threading.Lock()
_alerts: deque = deque(maxlen=MAX_ALERTS)            # newest at right
_by_camera: Dict[str, Alert] = {}                    # latest per camera
_by_id: Dict[int, Alert]     = {}                    # lookup by img_id

# SSE subscriber queues (alerts)
_sse_queues: list = []
_sse_lock = threading.Lock()

# Live camera store – latest raw JPEG per camera (no persistence)
_live_lock = threading.Lock()
_live_jpeg: Dict[str, bytes] = {}   # prefix -> latest JPEG
_live_ts:   Dict[str, float] = {}   # prefix -> epoch timestamp

# SSE subscriber queues (live camera feed)
_live_sse_queues: list = []
_live_sse_lock = threading.Lock()


def _store_live(prefix: str, jpeg: bytes):
    ts = time.time()
    with _live_lock:
        _live_jpeg[prefix] = jpeg
        _live_ts[prefix]   = ts
    event = "data: " + json.dumps({"camera": prefix, "ts": ts}) + "\n\n"
    with _live_sse_lock:
        dead = []
        for q in _live_sse_queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _live_sse_queues.remove(q)


def _store_alert(alert: Alert):
    with _lock:
        _alerts.append(alert)
        _by_camera[alert.camera] = alert
        _by_id[alert.img_id] = alert
        # Prune old id entries to avoid unbounded growth
        if len(_by_id) > MAX_ALERTS * 2:
            oldest_ids = [a.img_id for a in list(_alerts)]
            _by_id.clear()
            for a in _alerts:
                _by_id[a.img_id] = a
    # Notify SSE subscribers
    event = "data: " + json.dumps(alert.to_dict()) + "\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


# ─── MQTT chunk reassembly ────────────────────────────────────────────────────

@dataclass
class _ImgBuf:
    prefix:  str
    img_id:  int
    chunks:  int
    dark:    bool
    ts:      float = field(default_factory=time.monotonic)
    data:    Dict[int, bytes] = field(default_factory=dict)
    meta:    dict = field(default_factory=dict)

    def complete(self) -> bool:
        return len(self.data) == self.chunks

    def assemble(self) -> bytes:
        return b"".join(self.data[i] for i in range(self.chunks))


_bufs: Dict[Tuple[str, int], _ImgBuf] = {}
_raw_bufs: Dict[Tuple[str, int], _ImgBuf] = {}


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("MQTT connected to %s:%d", MQTT_BROKER, MQTT_PORT)
        # Alert image topics (annotated, from detector)
        client.subscribe("+/alert/image/begin", 0)
        client.subscribe("+/+/alert/image/begin", 0)
        client.subscribe("+/alert/image/data", 0)
        client.subscribe("+/+/alert/image/data", 0)
        client.subscribe("+/alert/image/end", 0)
        client.subscribe("+/+/alert/image/end", 0)
        client.subscribe("+/alert", 0)
        client.subscribe("+/+/alert", 0)
        # Raw camera image topics (all frames from the camera)
        client.subscribe("+/image/begin", 0)
        client.subscribe("+/+/image/begin", 0)
        client.subscribe("+/image/data", 0)
        client.subscribe("+/+/image/data", 0)
        client.subscribe("+/image/end", 0)
        client.subscribe("+/+/image/end", 0)
    else:
        log.error("MQTT connect failed rc=%d", rc)


def _on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning("MQTT disconnected rc=%d – reconnecting", rc)


def _on_message(client, userdata, msg: mqtt.MQTTMessage):
    topic = msg.topic
    parts = topic.split("/")
    action = parts[-1]

    try:
        # ── alert JSON (summary with metadata) ──
        if action == "alert" and len(parts) >= 2:
            meta = json.loads(msg.payload)
            prefix = "/".join(parts[:-1])
            # Store meta for when the image arrives
            key = (prefix, int(meta.get("image_id", 0)))
            if key in _bufs:
                _bufs[key].meta = meta
            return

        # ── alert image begin / data / end ──
        # Topic format: <prefix>/alert/image/<action>
        if len(parts) >= 4 and parts[-2] == "image" and parts[-3] == "alert":
            prefix = "/".join(parts[:-3])   # e.g. "cam/01"

            if action == "begin":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                key    = (prefix, img_id)
                _bufs[key] = _ImgBuf(
                    prefix=prefix,
                    img_id=img_id,
                    chunks=int(meta["chunks"]),
                    dark=bool(meta.get("dark", 0)),
                )

            elif action == "data":
                if len(msg.payload) < 6:
                    return
                img_id = struct.unpack_from(">I", msg.payload, 0)[0]
                idx    = struct.unpack_from(">H", msg.payload, 4)[0]
                key    = (prefix, img_id)
                if key in _bufs:
                    _bufs[key].data[idx] = bytes(msg.payload[6:])

            elif action == "end":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                key    = (prefix, img_id)
                buf    = _bufs.pop(key, None)
                if buf is None or not meta.get("ok", 0):
                    return
                if not buf.complete():
                    log.warning("[%s] Incomplete alert image #%d", prefix, img_id)
                    return

                jpeg     = buf.assemble()
                buf_meta = buf.meta
                ts       = time.time()
                alert    = Alert(
                    img_id=img_id,
                    camera=prefix,
                    ts_iso=buf_meta.get("ts_iso",
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))),
                    ts=buf_meta.get("ts", ts),
                    persons=buf_meta.get("persons", 0),
                    dark=buf.dark,
                    jpeg=jpeg,
                )
                _store_alert(alert)
                log.info("[%s] Alert #%d stored (%d B, %d person(s))",
                         prefix, img_id, len(jpeg), alert.persons)

        # ── raw camera image begin / data / end ──
        # Topic format: <prefix>/image/<action>  (no "alert" segment)
        elif len(parts) >= 3 and parts[-2] == "image" and parts[-3] != "alert":
            prefix = "/".join(parts[:-2])   # e.g. "cam/01"

            if action == "begin":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                key    = (prefix, img_id)
                _raw_bufs[key] = _ImgBuf(
                    prefix=prefix,
                    img_id=img_id,
                    chunks=int(meta["chunks"]),
                    dark=bool(meta.get("dark", 0)),
                )

            elif action == "data":
                if len(msg.payload) < 6:
                    return
                img_id = struct.unpack_from(">I", msg.payload, 0)[0]
                idx    = struct.unpack_from(">H", msg.payload, 4)[0]
                key    = (prefix, img_id)
                if key in _raw_bufs:
                    _raw_bufs[key].data[idx] = bytes(msg.payload[6:])

            elif action == "end":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                key    = (prefix, img_id)
                buf    = _raw_bufs.pop(key, None)
                if buf is None or not meta.get("ok", 0):
                    return
                if not buf.complete():
                    log.debug("[%s] Incomplete raw image #%d", prefix, img_id)
                    return
                _store_live(prefix, buf.assemble())
                log.debug("[%s] Live frame #%d (%d B)", prefix, img_id, buf.chunks * CHUNK_SIZE)

    except Exception:
        log.exception("Error handling topic '%s'", topic)


def _start_mqtt():
    client = mqtt.Client(
        client_id=MQTT_CLIENT,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS or None)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message    = _on_message

    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            log.error("MQTT error: %s – retry in 10 s", e)
            time.sleep(10)


# ─── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Shared HTML snippets ──────────────────────────────────────────────────────
_STYLE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#c9d1d9}
header{background:#161b22;border-bottom:1px solid #30363d;padding:.6em 1em;
       display:flex;align-items:center;gap:.8em}
header h1{font-size:1.1em;font-weight:600;color:#58a6ff}
header .dot{width:10px;height:10px;border-radius:50%;background:#3fb950;
            animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
      gap:1em;padding:1em}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;
      overflow:hidden;transition:border-color .2s}
.card:hover{border-color:#58a6ff}
.card img{width:100%;display:block;object-fit:cover;max-height:240px}
.card .info{padding:.55em .7em;font-size:.8em;line-height:1.6;color:#8b949e}
.card .info b{color:#c9d1d9}
.tag{display:inline-block;background:#21262d;border:1px solid #30363d;
     border-radius:4px;padding:.1em .4em;font-size:.75em;margin-right:.3em}
.tag.dark{border-color:#6e40c9;color:#d2a8ff}
.tag.person{border-color:#3fb950;color:#56d364}
#empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
       min-height:60vh;color:#484f58;text-align:center}
#empty svg{margin-bottom:.8em;opacity:.3}
</style>
"""

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamSec – Alertes</title>
""" + _STYLE + """
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>&#128247; CamSec – Alertes de presència</h1>
  <span style="margin-left:auto;font-size:.8em;color:#484f58" id="counter">0 alertes</span>
</header>
<div id="grid">
  <div id="empty">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48
               10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
    </svg>
    <p>Esperant alertes de detecció…</p>
    <p style="font-size:.8em;margin-top:.4em">Les alertes apareixeran aquí en temps real quan es detecti una persona.</p>
  </div>
</div>
<script>
const grid    = document.getElementById('grid');
const empty   = document.getElementById('empty');
const counter = document.getElementById('counter');
const dot     = document.getElementById('dot');
let count     = 0;

// Load existing alerts on page load
fetch('/api/alerts').then(r=>r.json()).then(data=>{
  data.reverse().forEach(addCard);
});

// Subscribe to real-time SSE stream
const es = new EventSource('/stream');
es.onmessage = e => {
  const alert = JSON.parse(e.data);
  addCard(alert, true);
};
es.onerror = () => { dot.style.background='#f85149'; };
es.onopen  = () => { dot.style.background='#3fb950'; };

function addCard(a, prepend=false) {
  if (empty.parentNode === grid) grid.removeChild(empty);
  count++;
  counter.textContent = count + (count===1?' alerta':' alertes');

  const card = document.createElement('div');
  card.className = 'card';
  const tags = (a.dark ? '<span class="tag dark">&#127769; Poca llum</span>' : '')
             + '<span class="tag person">&#128100; '+ a.persons +' persona(es)</span>';
  card.innerHTML = `
    <a href="${a.url}" target="_blank">
      <img src="${a.url}" loading="lazy" alt="Alerta ${a.img_id}">
    </a>
    <div class="info">
      <div>${tags}</div>
      <div style="margin-top:.4em">
        <b>Càmera:</b> ${a.camera}<br>
        <b>Data:</b> ${a.ts_iso.replace('T',' ').replace('Z',' UTC')}<br>
        <b>Imatge ID:</b> ${a.img_id}
      </div>
    </div>`;
  if (prepend && grid.firstChild) {
    grid.insertBefore(card, grid.firstChild);
  } else {
    grid.appendChild(card);
  }
}
</script>
</body>
</html>
"""

_EMBED_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamSec embed</title>
""" + _STYLE + """
<style>
body{background:#0d1117}
#grid{padding:.5em;gap:.5em}
.card img{max-height:180px}
</style>
</head>
<body>
<div id="grid"></div>
<script>
const grid = document.getElementById('grid');
let count  = 0;
fetch('/api/alerts?limit=6').then(r=>r.json()).then(data=>{
  data.slice(0,6).reverse().forEach(addCard);
});
const es = new EventSource('/stream');
es.onmessage = e => {
  addCard(JSON.parse(e.data), true);
  // keep only last 6 cards
  while(grid.children.length>6) grid.removeChild(grid.lastChild);
};
function addCard(a, prepend=false){
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<a href="${a.url}" target="_blank">
    <img src="${a.url}" loading="lazy"></a>
    <div class="info"><b>${a.camera}</b> &nbsp; ${a.ts_iso.replace('T',' ').replace('Z','')}
    &nbsp; &#128100;${a.persons}</div>`;
  if(prepend && grid.firstChild) grid.insertBefore(card,grid.firstChild);
  else grid.appendChild(card);
}
</script>
</body>
</html>
"""

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return Response(_DASHBOARD_HTML, mimetype="text/html")


@app.route("/embed")
def embed():
    """Minimal panel — embed via <iframe src='http://SERVER:8080/embed'>"""
    return Response(_EMBED_HTML, mimetype="text/html")


@app.route("/stream")
def stream():
    """Server-Sent Events – push new alerts to subscribed browsers."""
    q: queue.Queue = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_queues.append(q)

    def generate():
        # Send a comment every 25 s to keep the connection alive
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield data
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering
        },
    )


@app.route("/api/alerts")
def api_alerts():
    """Return list of recent alerts (newest first). ?limit=N to restrict."""
    from flask import request
    limit = min(int(request.args.get("limit", MAX_ALERTS)), MAX_ALERTS)
    with _lock:
        items = [a.to_dict() for a in reversed(list(_alerts))][:limit]
    return jsonify(items)


@app.route("/api/alerts/latest")
def api_latest():
    """Return the most recent alert per camera."""
    with _lock:
        items = [a.to_dict() for a in _by_camera.values()]
    return jsonify(items)


@app.route("/api/image/<int:img_id>")
def api_image(img_id: int):
    """Serve the annotated JPEG for a given alert image id."""
    with _lock:
        alert = _by_id.get(img_id)
    if alert is None:
        abort(404)
    return send_file(
        io.BytesIO(alert.jpeg),
        mimetype="image/jpeg",
        download_name=f"alert_{img_id}.jpg",
    )


_LIVE_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamSec – Directe</title>
""" + _STYLE + """
<style>
.card img{max-height:280px;cursor:default}
.card .info{padding:.45em .7em;font-size:.78em;color:#8b949e;line-height:1.5}
.card .info b{color:#c9d1d9}
#empty p{font-size:.85em;margin-top:.4em}
nav a{color:#58a6ff;font-size:.85em;text-decoration:none}
nav a:hover{text-decoration:underline}
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>&#128247; CamSec – Directe</h1>
  <nav style="margin-left:auto;display:flex;gap:1em">
    <a href="/">Alertes</a>
    <a href="/live">Directe</a>
    <a href="/help">Ajuda</a>
  </nav>
</header>
<div id="grid">
  <div id="empty">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="currentColor">
      <path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45
               1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/>
    </svg>
    <p>Esperant imatges de les càmeres…</p>
    <p>S'actualitzen automàticament a mesura que arriben noves imatges.</p>
  </div>
</div>
<script>
const grid = document.getElementById('grid');
const dot  = document.getElementById('dot');
const cards = {};

// Load existing cameras
fetch('/api/live/latest').then(r=>r.json()).then(cams=>{
  cams.forEach(c => updateCard(c.camera, c.ts_iso));
});

const es = new EventSource('/live/stream');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  updateCard(d.camera, new Date(d.ts * 1000).toISOString().replace('T',' ').replace('Z',' UTC'));
};
es.onerror = () => { dot.style.background='#f85149'; };
es.onopen  = () => { dot.style.background='#3fb950'; };

function updateCard(camera, tsIso) {
  const empty = document.getElementById('empty');
  if (empty) empty.remove();

  if (cards[camera]) {
    // Refresh image by busting cache
    const img = cards[camera].querySelector('img');
    const url = '/api/live/image/' + camera + '?t=' + Date.now();
    img.src = url;
    cards[camera].querySelector('.ts').textContent = tsIso || '';
    return;
  }

  const card = document.createElement('div');
  card.className = 'card';
  const url = '/api/live/image/' + camera + '?t=' + Date.now();
  card.innerHTML = `
    <img src="${url}" alt="${camera}" onerror="this.style.opacity='.3'">
    <div class="info">
      <b>${camera}</b><br>
      <span class="ts">${tsIso || ''}</span>
    </div>`;
  cards[camera] = card;
  grid.appendChild(card);
}
</script>
</body>
</html>
"""


@app.route("/live")
def live():
    """Live camera view – shows the latest raw frame from every camera."""
    return Response(_LIVE_HTML, mimetype="text/html")


@app.route("/live/stream")
def live_stream():
    """SSE stream – fires an event whenever a new raw camera frame arrives."""
    q: queue.Queue = queue.Queue(maxsize=50)
    with _live_sse_lock:
        _live_sse_queues.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield data
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            with _live_sse_lock:
                if q in _live_sse_queues:
                    _live_sse_queues.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/live/latest")
def api_live_latest():
    """Return the list of cameras that have a live frame, with their last timestamp."""
    with _live_lock:
        result = [
            {"camera": cam, "ts": _live_ts[cam],
             "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_live_ts[cam])),
             "url": f"/api/live/image/{cam}"}
            for cam in _live_jpeg
        ]
    return jsonify(result)


@app.route("/api/live/image/<path:prefix>")
def api_live_image(prefix: str):
    """Serve the latest raw JPEG for a given camera prefix."""
    with _live_lock:
        jpeg = _live_jpeg.get(prefix)
    if jpeg is None:
        abort(404)
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


HELP_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CamSec Viewer – Ajuda</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      padding: 2rem 1rem;
    }
    a { color: #38bdf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .container { max-width: 860px; margin: 0 auto; }
    h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: 0.25rem; color: #f8fafc; }
    .subtitle { color: #94a3b8; margin-bottom: 2rem; font-size: 0.95rem; }
    h2 {
      font-size: 1.1rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.06em; color: #94a3b8;
      margin: 2rem 0 0.75rem;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid #1e293b;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    thead th {
      text-align: left; padding: 0.5rem 0.75rem;
      background: #1e293b; color: #94a3b8;
      font-weight: 600; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em;
    }
    tbody tr:nth-child(even) { background: #0f1f35; }
    tbody tr:hover { background: #1e3a5f; }
    td { padding: 0.55rem 0.75rem; vertical-align: top; border-bottom: 1px solid #1e293b; }
    .badge {
      display: inline-block; padding: 0.15rem 0.5rem;
      border-radius: 0.3rem; font-size: 0.75rem; font-weight: 700;
      font-family: monospace; letter-spacing: 0.03em;
    }
    .get    { background: #14532d; color: #86efac; }
    .delete { background: #7f1d1d; color: #fca5a5; }
    .mono   { font-family: monospace; font-size: 0.85rem; color: #7dd3fc; }
    .default{ font-family: monospace; font-size: 0.82rem; color: #a78bfa; }
    .back   { margin-top: 2.5rem; font-size: 0.85rem; color: #64748b; }
  </style>
</head>
<body>
<div class="container">
  <h1>CamSec Viewer</h1>
  <p class="subtitle">Documentació d'endpoints i variables de configuració</p>

  <h2>Endpoints REST</h2>
  <table>
    <thead>
      <tr><th>Mètode</th><th>Ruta</th><th>Descripció</th></tr>
    </thead>
    <tbody>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/live">/live</a></td>
        <td>Vista en directe de totes les càmeres (totes les imatges, no només alertes)</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/live/stream">/live/stream</a></td>
        <td>SSE en temps real de les imatges raw de les càmeres</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><span class="mono">/api/live/latest</span></td>
        <td>Llista de càmeres amb el seu darrer frame (JSON)</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><span class="mono">/api/live/image/&lt;prefix&gt;</span></td>
        <td>Serveix l'últim JPEG en brut d'una càmera concreta</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/">/</a></td>
        <td>Dashboard HTML amb actualització en temps real</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/embed">/embed</a></td>
        <td>Panel mínim per incloure via <code>&lt;iframe&gt;</code></td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/stream">/stream</a></td>
        <td>Server-Sent Events: rebrà cada nova alerta com a JSON</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/api/alerts">/api/alerts</a></td>
        <td>Llista d'alertes recents en memòria (JSON). Paràmetre opcional: <code>?limit=N</code></td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/api/alerts/latest">/api/alerts/latest</a></td>
        <td>Darrera alerta per càmera (JSON)</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><span class="mono">/api/alerts/camera/&lt;prefix&gt;</span></td>
        <td>Historial complet d'una càmera (des de disc)</td>
      </tr>
      <tr>
        <td><span class="badge delete">DELETE</span></td>
        <td><span class="mono">/api/alerts/camera/&lt;prefix&gt;</span></td>
        <td>Elimina totes les fotos d'una càmera (RAM + disc)</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><span class="mono">/api/image/&lt;id&gt;</span></td>
        <td>Serveix el JPEG d'una alerta concreta</td>
      </tr>
      <tr>
        <td><span class="badge delete">DELETE</span></td>
        <td><span class="mono">/api/image/&lt;id&gt;</span></td>
        <td>Elimina una foto concreta (RAM + disc)</td>
      </tr>
      <tr>
        <td><span class="badge get">GET</span></td>
        <td><a class="mono" href="/help">/help</a></td>
        <td>Aquesta pàgina d'ajuda</td>
      </tr>
    </tbody>
  </table>

  <h2>Variables de configuració</h2>
  <table>
    <thead>
      <tr><th>Variable</th><th>Per defecte</th><th>Descripció</th></tr>
    </thead>
    <tbody>
      <tr><td class="mono">MQTT_BROKER</td> <td class="default">localhost</td>    <td>IP o hostname del broker MQTT</td></tr>
      <tr><td class="mono">MQTT_PORT</td>   <td class="default">1883</td>         <td>Port TCP del broker MQTT</td></tr>
      <tr><td class="mono">MQTT_USER</td>   <td class="default">""</td>           <td>Usuari MQTT (opcional)</td></tr>
      <tr><td class="mono">MQTT_PASS</td>   <td class="default">""</td>           <td>Contrasenya MQTT (opcional)</td></tr>
      <tr><td class="mono">MQTT_CLIENT</td> <td class="default">camsec-viewer</td><td>Client ID MQTT</td></tr>
      <tr><td class="mono">CHUNK_SIZE</td>  <td class="default">4096</td>         <td>Mida del chunk en bytes (ha de coincidir amb la càmera)</td></tr>
      <tr><td class="mono">MAX_ALERTS</td>  <td class="default">50</td>           <td>Màxim d'alertes conservades en memòria RAM</td></tr>
      <tr><td class="mono">HTTP_HOST</td>   <td class="default">0.0.0.0</td>      <td>Adreça d'escolta del servidor HTTP</td></tr>
      <tr><td class="mono">HTTP_PORT</td>   <td class="default">8080</td>         <td>Port d'escolta del servidor HTTP</td></tr>
      <tr><td class="mono">LOG_LEVEL</td>   <td class="default">INFO</td>         <td>Nivell de log: DEBUG · INFO · WARNING · ERROR</td></tr>
    </tbody>
  </table>

  <p class="back"><a href="/">← Torna al dashboard</a></p>
</div>
</body>
</html>
"""

@app.route("/help")
def help_endpoint():
    """Return an HTML page describing all available endpoints and config variables."""
    return Response(HELP_HTML, mimetype="text/html")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start MQTT listener in a background daemon thread
    t = threading.Thread(target=_start_mqtt, daemon=True, name="mqtt")
    t.start()

    log.info("HTTP server listening on %s:%d", HTTP_HOST, HTTP_PORT)
    # Use threaded=True so SSE clients don't block each other
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
