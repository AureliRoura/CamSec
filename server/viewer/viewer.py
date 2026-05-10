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
LOG_LEVEL        = os.getenv("LOG_LEVEL",        "INFO")
PERSIST_DIR      = os.getenv("PERSIST_DIR",      "/data/alerts")
MAX_DISK_ALERTS  = int(os.getenv("MAX_DISK_ALERTS", "500"))
MAX_DISK_DAYS    = int(os.getenv("MAX_DISK_DAYS",   "7"))
_INDEX_FILE      = os.path.join(PERSIST_DIR, "index.jsonl")

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
    jpeg:      bytes        # annotated JPEG (empty for disk-only alerts)
    jpeg_path: str  = ""   # path on disk (set after persisting)
    b64:       str  = ""   # base64 for JSON API (filled lazily)

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

# ─── Disk persistence ────────────────────────────────────────────────────────
_disk_write_count = 0


def _img_path(img_id: int) -> str:
    return os.path.join(PERSIST_DIR, "images", f"{img_id}.jpg")


def _entry_to_dict(e: dict) -> dict:
    """Convert an index.jsonl entry to the same format as Alert.to_dict()."""
    return {
        "img_id":  e["img_id"],
        "camera":  e["camera"],
        "ts_iso":  e.get("ts_iso", ""),
        "ts":      e.get("ts", 0.0),
        "persons": e.get("persons", 0),
        "dark":    e.get("dark", False),
        "url":     f"/api/image/{e['img_id']}",
    }


def _read_index() -> list:
    """Read all entries from index.jsonl, sorted newest first."""
    if not os.path.exists(_INDEX_FILE):
        return []
    entries = []
    try:
        with open(_INDEX_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    entries.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return entries


def _persist_alert(alert: Alert) -> None:
    """Save JPEG to disk and append metadata to index.jsonl."""
    if not alert.jpeg:
        return
    try:
        img_dir = os.path.join(PERSIST_DIR, "images")
        os.makedirs(img_dir, exist_ok=True)
        path = _img_path(alert.img_id)
        with open(path, "wb") as f:
            f.write(alert.jpeg)
        alert.jpeg_path = path
        entry = {
            "img_id":  alert.img_id,
            "camera":  alert.camera,
            "ts":      alert.ts,
            "ts_iso":  alert.ts_iso,
            "persons": alert.persons,
            "dark":    alert.dark,
            "path":    path,
        }
        with open(_INDEX_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not persist alert #%d: %s", alert.img_id, exc)


def _rotate_disk() -> None:
    """Enforce MAX_DISK_DAYS / MAX_DISK_ALERTS retention (runs every 10 saves)."""
    global _disk_write_count
    _disk_write_count += 1
    if _disk_write_count % 10 != 0:
        return
    if not os.path.exists(_INDEX_FILE):
        return
    cutoff = time.time() - MAX_DISK_DAYS * 86400
    entries = []
    try:
        with open(_INDEX_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return
    keep   = [e for e in entries if e.get("ts", 0) >= cutoff]
    remove = [e for e in entries if e.get("ts", 0) < cutoff]
    keep.sort(key=lambda x: x.get("ts", 0))
    if len(keep) > MAX_DISK_ALERTS:
        remove += keep[:-MAX_DISK_ALERTS]
        keep    = keep[-MAX_DISK_ALERTS:]
    for e in remove:
        p = e.get("path", "")
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    if remove:
        log.info("Rotated %d old alert(s) from disk", len(remove))
    try:
        with open(_INDEX_FILE, "w") as f:
            for e in keep:
                f.write(json.dumps(e) + "\n")
    except OSError as exc:
        log.warning("Could not rewrite index: %s", exc)


def _load_persisted() -> None:
    """Populate in-memory alert store from disk on startup."""
    os.makedirs(os.path.join(PERSIST_DIR, "images"), exist_ok=True)
    entries = _read_index()  # newest first
    if not entries:
        log.info("No persisted alerts found in %s", PERSIST_DIR)
        return
    log.info("Loading %d persisted alerts from disk", len(entries))
    # Insert oldest-of-recent first so the newest ends up at the right of the deque
    recent = list(reversed(entries[:MAX_ALERTS]))
    with _lock:
        for e in recent:
            alert = Alert(
                img_id=e["img_id"],
                camera=e["camera"],
                ts_iso=e.get("ts_iso", ""),
                ts=e.get("ts", 0.0),
                persons=e.get("persons", 0),
                dark=bool(e.get("dark", False)),
                jpeg=b"",
                jpeg_path=e.get("path", ""),
            )
            _alerts.append(alert)
            _by_id[alert.img_id] = alert
        # _by_camera = most recent per camera (entries is newest-first)
        for e in entries:
            cam = e["camera"]
            if cam not in _by_camera and e["img_id"] in _by_id:
                _by_camera[cam] = _by_id[e["img_id"]]


# SSE subscriber queues
_sse_queues: list = []
_sse_lock = threading.Lock()


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
    # Persist to disk (outside lock)
    _persist_alert(alert)
    _rotate_disk()
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
_pending_meta: Dict[Tuple[str, int], dict] = {}  # alert summary may arrive before begin


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("MQTT connected to %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe("+/alert/image/begin", 0)
        client.subscribe("+/+/alert/image/begin", 0)
        client.subscribe("+/alert/image/data", 0)
        client.subscribe("+/+/alert/image/data", 0)
        client.subscribe("+/alert/image/end", 0)
        client.subscribe("+/+/alert/image/end", 0)
        client.subscribe("+/alert", 0)
        client.subscribe("+/+/alert", 0)
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
            key = (prefix, int(meta.get("image_id", 0)))
            # May arrive before begin; store in pending dict as well
            _pending_meta[key] = meta
            if key in _bufs:
                _bufs[key].meta = meta
            return

        # ── alert image begin / data / end ──
        # Topic format: <prefix>/alert/image/<action>
        if len(parts) < 4 or parts[-2] != "image" or parts[-3] != "alert":
            return

        prefix = "/".join(parts[:-3])   # e.g. "cam/01"

        if action == "begin":
            meta   = json.loads(msg.payload)
            img_id = int(meta["id"])
            key    = (prefix, img_id)
            buf = _ImgBuf(
                prefix=prefix,
                img_id=img_id,
                chunks=int(meta["chunks"]),
                dark=bool(meta.get("dark", 0)),
            )
            # Attach pending alert summary if it already arrived
            if key in _pending_meta:
                buf.meta = _pending_meta.pop(key)
            _bufs[key] = buf

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
            # Fallback to pending meta if begin arrived before alert summary
            buf_meta = buf.meta if buf.meta else _pending_meta.pop(key, {})
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
.dot{width:10px;height:10px;border-radius:50%;background:#3fb950;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── Camera grid ── */
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
      gap:1em;padding:1em}
.cam-card{background:#161b22;border:1px solid #30363d;border-radius:8px;
          overflow:hidden;cursor:pointer;transition:border-color .2s,transform .15s}
.cam-card:hover{border-color:#58a6ff;transform:translateY(-2px)}
.cam-card img{width:100%;display:block;object-fit:cover;max-height:220px}
.cam-card .bar{padding:.55em .7em;display:flex;align-items:center;gap:.5em;
               font-size:.82em;background:#161b22}
.cam-card .cam-name{font-weight:600;color:#c9d1d9;flex:1;overflow:hidden;
                    text-overflow:ellipsis;white-space:nowrap}
.cam-card .ts{color:#484f58;font-size:.75em}
.tag{display:inline-block;background:#21262d;border:1px solid #30363d;
     border-radius:4px;padding:.1em .4em;font-size:.72em}
.tag.dark{border-color:#6e40c9;color:#d2a8ff}
.tag.person{border-color:#3fb950;color:#56d364}
.hint{font-size:.72em;color:#484f58;padding:.3em .7em .55em;text-align:right}

#empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
       min-height:60vh;color:#484f58;text-align:center}
#empty svg{margin-bottom:.8em;opacity:.3}

/* ── History modal ── */
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);
       z-index:100;overflow-y:auto}
#modal.open{display:block}
#modal-box{background:#161b22;border:1px solid #30363d;border-radius:10px;
           max-width:900px;margin:2em auto;padding:0;overflow:hidden}
#modal-header{background:#21262d;padding:.7em 1em;display:flex;
              align-items:center;gap:.6em;border-bottom:1px solid #30363d}
#modal-title{font-size:1em;font-weight:600;color:#58a6ff;flex:1}
#modal-close{background:none;border:none;color:#8b949e;font-size:1.3em;
             cursor:pointer;padding:.2em .4em;border-radius:4px}
#modal-close:hover{background:#30363d;color:#c9d1d9}
#modal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
            gap:.8em;padding:1em}
.hist-card{background:#0d1117;border:1px solid #21262d;border-radius:6px;overflow:hidden}
.hist-card img{width:100%;display:block;object-fit:cover;max-height:170px;cursor:zoom-in}
.hist-card .info{padding:.4em .6em;font-size:.75em;color:#8b949e;line-height:1.5}
.hist-card .del-one{display:block;width:100%;padding:.3em;background:none;
  border:none;border-top:1px solid #21262d;color:#6e7681;font-size:.75em;
  cursor:pointer;text-align:center}
.hist-card .del-one:hover{background:#f851491a;color:#f85149}
.hist-card.removing{opacity:.3;pointer-events:none;transition:opacity .3s}
#del-camera{background:none;border:1px solid #f85149;color:#f85149;
  border-radius:5px;padding:.3em .8em;font-size:.8em;cursor:pointer}
#del-camera:hover{background:#f85149;color:#fff}
#modal-empty{padding:2em;text-align:center;color:#484f58}
</style>
"""

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamSec – Càmeres</title>
""" + _STYLE + """
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>&#128247; CamSec</h1>
  <span style="margin-left:auto;font-size:.8em;color:#484f58" id="counter">0 càmeres</span>
</header>

<div id="grid">
  <div id="empty">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48
               10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
    </svg>
    <p>Esperant alertes de detecció…</p>
    <p style="font-size:.8em;margin-top:.4em">Les càmeres apareixeran aquí quan s'enviï la primera alerta.</p>
  </div>
</div>

<!-- History modal -->
<div id="modal">
  <div id="modal-box">
    <div id="modal-header">
      <span id="modal-title">Historial</span>
      <button id="del-camera" onclick="deleteCameraAlerts()" title="Elimina totes les fotos d'aquesta c\u00e0mera">&#128465; Elimina totes</button>
      <button id="modal-close" onclick="closeModal()">&#x2715;</button>
    </div>
    <div id="modal-grid"></div>
  </div>
</div>

<script>
const grid    = document.getElementById('grid');
const empty   = document.getElementById('empty');
const counter = document.getElementById('counter');
const dot     = document.getElementById('dot');

// camera → latest alert object
const cameras = {};

function fmtTs(iso) {
  return iso.replace('T',' ').replace('Z',' UTC');
}

function buildTags(a) {
  return (a.dark ? '<span class="tag dark">&#127769; Poca llum</span> ' : '')
       + '<span class="tag person">&#128100; '+ a.persons +' persona(es)</span>';
}

function updateCard(a) {
  const existing = document.getElementById('cam-' + a.camera);
  if (existing) {
    existing.querySelector('img').src = a.url + '?t=' + Date.now();
    existing.querySelector('.ts').textContent = fmtTs(a.ts_iso);
    existing.querySelector('.tags').innerHTML = buildTags(a);
  } else {
    if (empty.parentNode === grid) grid.removeChild(empty);
    const card = document.createElement('div');
    card.className = 'cam-card';
    card.id = 'cam-' + a.camera;
    card.onclick = () => openHistory(a.camera);
    card.innerHTML =
      '<img src="'+ a.url +'" loading="lazy" alt="'+ a.camera +'">' +
      '<div class="bar">' +
        '<span class="cam-name">&#128247; '+ a.camera +'</span>' +
        '<span class="ts">'+ fmtTs(a.ts_iso) +'</span>' +
      '</div>' +
      '<div style="padding:.3em .7em .2em" class="tags">'+ buildTags(a) +'</div>' +
      '<div class="hint">Clica per veure l&#39;historial</div>';
    grid.appendChild(card);
    counter.textContent = grid.children.length + ' càmera' + (grid.children.length===1?'':'es');
  }
  cameras[a.camera] = a;
}

// Load latest per camera on page load
fetch('/api/alerts/latest').then(r=>r.json()).then(data=>{
  data.forEach(updateCard);
});

// Real-time SSE
const es = new EventSource('/stream');
es.onmessage = e => { updateCard(JSON.parse(e.data)); };
es.onerror   = () => { dot.style.background='#f85149'; };
es.onopen    = () => { dot.style.background='#3fb950'; };

// ── History modal ──────────────────────────────────────────────────────────
const modal      = document.getElementById('modal');
const modalGrid  = document.getElementById('modal-grid');
const modalTitle = document.getElementById('modal-title');
let   _curCamera = null;

function openHistory(camera) {
  _curCamera = camera;
  modalTitle.textContent = '📷 ' + camera + ' – Historial';
  loadHistory(camera);
  modal.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function loadHistory(camera) {
  modalGrid.innerHTML = '<p id="modal-empty">Carregant\u2026</p>';
  fetch('/api/alerts/camera/' + encodeURIComponent(camera))
    .then(r => r.json())
    .then(data => {
      if (!data.length) {
        modalGrid.innerHTML = '<p id="modal-empty">Sense historial disponible.</p>';
        return;
      }
      modalGrid.innerHTML = '';
      data.forEach(a => {
        const c = document.createElement('div');
        c.className = 'hist-card';
        c.dataset.imgId = a.img_id;
        c.innerHTML =
          '<a href="'+ a.url +'" target="_blank">'+
            '<img src="'+ a.url +'" loading="lazy">'+
          '</a>'+
          '<div class="info">'+ buildTags(a) +'<br>'+ fmtTs(a.ts_iso) +'</div>'+
          '<button class="del-one" onclick="deleteOne('+ a.img_id +', this)">'+
            '🗑️ Elimina aquesta foto</button>';
        modalGrid.appendChild(c);
      });
    });
}

function deleteOne(imgId, btn) {
  if (!confirm('Eliminar aquesta foto?')) return;
  const card = btn.closest('.hist-card');
  card.classList.add('removing');
  fetch('/api/image/' + imgId, {method: 'DELETE'})
    .then(r => {
      if (r.ok) {
        card.remove();
        if (!modalGrid.children.length)
          modalGrid.innerHTML = '<p id="modal-empty">Sense historial disponible.</p>';
      } else {
        card.classList.remove('removing');
        alert('Error eliminant la foto.');
      }
    })
    .catch(() => { card.classList.remove('removing'); alert('Error de xarxa.'); });
}

function deleteCameraAlerts() {
  if (!_curCamera) return;
  if (!confirm('Eliminar TOTES les fotos de ' + _curCamera + '?')) return;
  fetch('/api/alerts/camera/' + encodeURIComponent(_curCamera), {method: 'DELETE'})
    .then(r => {
      if (r.ok) {
        modalGrid.innerHTML = '<p id="modal-empty">Sense historial disponible.</p>';
        const card = document.getElementById('cam-' + _curCamera);
        if (card) card.remove();
        delete cameras[_curCamera];
        const n = Object.keys(cameras).length;
        counter.textContent = n + ' c\u00e0mera' + (n===1?'':'es');
      } else {
        alert('Error eliminant les fotos.');
      }
    })
    .catch(() => alert('Error de xarxa.'));
}

function closeModal() {
  modal.classList.remove('open');
  document.body.style.overflow = '';
}

modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
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


@app.route("/api/image/<int:img_id>", methods=["GET", "DELETE"])
def api_image(img_id: int):
    """GET: serve the annotated JPEG. DELETE: remove it from RAM and disk."""
    from flask import request as freq
    if freq.method == "DELETE":
        # Remove from in-memory stores
        with _lock:
            alert = _by_id.pop(img_id, None)
            if alert:
                try:
                    _alerts.remove(alert)
                except ValueError:
                    pass
                # Update _by_camera if this was the latest for its camera
                cam = alert.camera
                if _by_camera.get(cam) is alert:
                    remaining = [a for a in reversed(list(_alerts)) if a.camera == cam]
                    if remaining:
                        _by_camera[cam] = remaining[0]
                    else:
                        _by_camera.pop(cam, None)
        # Remove from disk
        path = _img_path(img_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        # Rewrite index without this entry
        try:
            entries = []
            if os.path.exists(_INDEX_FILE):
                with open(_INDEX_FILE, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                e = json.loads(line)
                                if e.get("img_id") != img_id:
                                    entries.append(e)
                            except json.JSONDecodeError:
                                pass
            with open(_INDEX_FILE, "w") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
        except OSError as exc:
            log.warning("Could not update index after delete: %s", exc)
        log.info("Deleted alert image #%d", img_id)
        return "", 204

    # GET
    with _lock:
        alert = _by_id.get(img_id)
    if alert is not None and alert.jpeg:
        return send_file(
            io.BytesIO(alert.jpeg),
            mimetype="image/jpeg",
            download_name=f"alert_{img_id}.jpg",
        )
    path = _img_path(img_id)
    if os.path.exists(path):
        return send_file(path, mimetype="image/jpeg",
                         download_name=f"alert_{img_id}.jpg")
    abort(404)


@app.route("/api/alerts/camera/<path:camera>", methods=["GET", "DELETE"])
def api_camera_alerts(camera: str):
    """GET: all alerts for camera from disk. DELETE: remove all."""
    from flask import request as freq
    if freq.method == "DELETE":
        entries = _read_index()
        to_remove = [e for e in entries if e.get("camera") == camera]
        keep      = [e for e in entries if e.get("camera") != camera]
        # Remove files
        for e in to_remove:
            p = e.get("path", "")
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # Rewrite index
        try:
            with open(_INDEX_FILE, "w") as f:
                for e in keep:
                    f.write(json.dumps(e) + "\n")
        except OSError as exc:
            log.warning("Could not rewrite index after camera delete: %s", exc)
        # Remove from in-memory stores
        ids_to_remove = {e["img_id"] for e in to_remove}
        with _lock:
            for img_id in ids_to_remove:
                _by_id.pop(img_id, None)
            new_alerts = deque((a for a in _alerts if a.camera != camera),
                               maxlen=MAX_ALERTS)
            _alerts.clear()
            _alerts.extend(new_alerts)
            _by_camera.pop(camera, None)
        log.info("Deleted all %d alert(s) for camera '%s'", len(to_remove), camera)
        return "", 204

    # GET
    entries = _read_index()
    items = [_entry_to_dict(e) for e in entries if e.get("camera") == camera]
    return jsonify(items)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load persisted alerts from disk before accepting connections
    _load_persisted()

    # Start MQTT listener in a background daemon thread
    t = threading.Thread(target=_start_mqtt, daemon=True, name="mqtt")
    t.start()

    log.info("HTTP server listening on %s:%d", HTTP_HOST, HTTP_PORT)
    from waitress import serve
    serve(app, host=HTTP_HOST, port=HTTP_PORT, threads=8,
          channel_timeout=3600)  # long timeout keeps SSE connections alive
