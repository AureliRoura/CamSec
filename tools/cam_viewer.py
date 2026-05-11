#!/usr/bin/env python3
"""
cam_viewer.py – Visor d'escriptori CamSec
==========================================
Mostra en temps real les imatges JPEG que les càmeres ESP32-CAM envien per MQTT.
Suporta múltiples càmeres alhora. Les imatges es mostren en una graella
que creix automàticament a mesura que apareixen càmeres noves.

Configuració (per ordre de prioritat):
  1. Arguments de línia de comandes (--broker, --port, --user, --password)
  2. Fitxer .env al mateix directori que l'script
  3. Variables d'entorn del sistema (MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS)

Ús ràpid:
    cp .env.example .env          # edita el .env amb la teva IP i credencials
    python cam_viewer.py

Ús amb arguments:
    python cam_viewer.py --broker <IP_BROKER> [--port 1883] [--user <USUARI>] [--password <CLAU>]

Dependències:
    pip install paho-mqtt Pillow
"""

import argparse
import io
import json
import os
import queue
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple


def _load_dotenv() -> None:
    """Carrega el .env del mateix directori que l'script (sense dependències externes)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

try:
    from PIL import Image, ImageTk
except ImportError:
    raise SystemExit("Pillow no instal·lat. Executa:  pip install Pillow")

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise SystemExit("paho-mqtt no instal·lat. Executa:  pip install paho-mqtt")


# ─── Configuració visual ──────────────────────────────────────────────────────

CELL_W    = 480     # amplada màxima de cada cel·la
CELL_H    = 320     # alçada màxima de la imatge
MAX_COLS  = 3       # màxim de columnes a la graella

BG        = "#0d1117"
HDR_BG    = "#161b22"
BORDER    = "#30363d"
TXT       = "#c9d1d9"
MUTED     = "#484f58"
ACCENT    = "#58a6ff"
GREEN     = "#3fb950"
YELLOW    = "#d29922"


# ─── Reassemblatge de chunks ──────────────────────────────────────────────────

@dataclass
class _ImgBuf:
    prefix: str
    img_id: int
    chunks: int
    dark:   bool
    born:   float = field(default_factory=time.monotonic)
    data:   Dict[int, bytes] = field(default_factory=dict)

    def complete(self) -> bool:
        return len(self.data) == self.chunks

    def assemble(self) -> bytes:
        return b"".join(self.data[i] for i in range(self.chunks))


# ─── Widget d'una càmera ──────────────────────────────────────────────────────

class CameraCell(tk.Frame):
    """Targeta que mostra la darrera imatge rebuda d'una càmera."""

    def __init__(self, parent: tk.Widget, camera: str):
        super().__init__(parent, bg=HDR_BG, highlightthickness=1,
                         highlightbackground=BORDER)
        self.camera = camera

        # Capçalera
        hdr = tk.Frame(self, bg=HDR_BG)
        hdr.pack(fill=tk.X, padx=8, pady=(6, 2))
        tk.Label(hdr, text=f"\u25CF  {camera}", bg=HDR_BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self._ts_lbl = tk.Label(hdr, text="\u2013", bg=HDR_BG, fg=MUTED,
                                font=("Segoe UI", 8))
        self._ts_lbl.pack(side=tk.RIGHT)

        # Àrea d'imatge
        self._canvas = tk.Label(self, bg=BG, width=CELL_W, height=CELL_H,
                                text="Esperant primera imatge\u2026",
                                fg=MUTED, font=("Segoe UI", 9))
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        # Barra inferior
        self._info = tk.Label(self, text="", bg=HDR_BG, fg=MUTED,
                              font=("Segoe UI", 8), anchor="w")
        self._info.pack(fill=tk.X, padx=8, pady=(2, 5))

        self._photo: Optional[ImageTk.PhotoImage] = None
        self._frame_count = 0

    def update_frame(self, jpeg: bytes, dark: bool) -> None:
        try:
            img = Image.open(io.BytesIO(jpeg))
            orig_w, orig_h = img.size
            img.thumbnail((CELL_W - 8, CELL_H), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self._canvas.configure(image=self._photo, text="")
            self._frame_count += 1
            ts = time.strftime("%H:%M:%S")
            self._ts_lbl.configure(text=ts)
            dark_txt = "  \U0001F319 Poca llum" if dark else ""
            self._info.configure(
                text=f"{orig_w}\u00d7{orig_h}  \u00b7  "
                     f"{len(jpeg) // 1024} KB  \u00b7  "
                     f"frame #{self._frame_count}{dark_txt}"
            )
        except Exception as exc:
            self._info.configure(text=f"Error: {exc}")


# ─── Aplicació principal ──────────────────────────────────────────────────────

class ViewerApp:
    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root  = root
        self.args  = args
        self._q: queue.Queue = queue.Queue()
        self._bufs: Dict[Tuple[str, int], _ImgBuf] = {}
        self._cells: Dict[str, CameraCell] = {}

        self._build_ui()
        threading.Thread(target=self._mqtt_thread, daemon=True, name="mqtt").start()
        self.root.after(50, self._poll)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.title("CamSec \u2013 Visor de c\u00e0meres")
        self.root.configure(bg=BG)
        self.root.minsize(CELL_W + 32, CELL_H + 110)

        # Capçalera
        hdr = tk.Frame(self.root, bg=HDR_BG)
        hdr.pack(fill=tk.X, side=tk.TOP)
        tk.Frame(hdr, bg=BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        tk.Label(hdr, text="\U0001F4F7  CamSec \u2013 Visor de c\u00e0meres",
                 bg=HDR_BG, fg=ACCENT,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT, padx=14, pady=8)

        self._dot = tk.Label(hdr, text="\u25CF", bg=HDR_BG, fg=MUTED,
                             font=("Segoe UI", 10))
        self._dot.pack(side=tk.RIGHT, padx=4)
        self._status_lbl = tk.Label(hdr, text="Connectant\u2026", bg=HDR_BG,
                                    fg=MUTED, font=("Segoe UI", 9))
        self._status_lbl.pack(side=tk.RIGHT, padx=(14, 2), pady=8)

        # Graella
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._grid = container

        # Missatge buit
        self._empty = tk.Label(
            self._grid,
            text="Esperant càmeres\u2026\n\nLes imatges apareixeran aquí quan\nla càmera comenci a enviar.",
            bg=BG, fg=MUTED, font=("Segoe UI", 11), justify=tk.CENTER
        )
        self._empty.grid(row=0, column=0, padx=20, pady=60)

    def _get_or_create_cell(self, camera: str) -> CameraCell:
        if camera not in self._cells:
            if self._empty.winfo_manager():
                self._empty.grid_remove()
            idx = len(self._cells)
            row, col = divmod(idx, MAX_COLS)
            cell = CameraCell(self._grid, camera)
            cell.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            self._grid.columnconfigure(col, weight=1)
            self._grid.rowconfigure(row, weight=1)
            self._cells[camera] = cell
        return self._cells[camera]

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self._status_lbl.configure(text=text, fg=color)
        self._dot.configure(fg=color)

    # ── Queue poll (UI thread) ───────────────────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "frame":
                    _, camera, jpeg, dark = msg
                    cell = self._get_or_create_cell(camera)
                    cell.update_frame(jpeg, dark)
                elif kind == "status":
                    self._set_status(msg[1], msg[2] if len(msg) > 2 else MUTED)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ── MQTT ────────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._q.put(("status", f"Error de connexi\u00f3 ({reason_code})", YELLOW))
            return
        self._q.put(("status",
                     f"Connectat a {self.args.broker}:{self.args.port}",
                     GREEN))
        for depth in ("+", "+/+"):
            client.subscribe(f"{depth}/image/begin", 0)
            client.subscribe(f"{depth}/image/data",  0)
            client.subscribe(f"{depth}/image/end",   0)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if reason_code.is_failure:
            self._q.put(("status", "Desconnectat \u2013 reconnectant\u2026", YELLOW))

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        parts  = msg.topic.split("/")
        action = parts[-1]
        if len(parts) < 3 or parts[-2] != "image":
            return
        # Ignora els topics d'alerta (…/alert/image/…)
        if len(parts) >= 4 and parts[-3] == "alert":
            return
        prefix = "/".join(parts[:-2])

        try:
            if action == "begin":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                self._bufs[(prefix, img_id)] = _ImgBuf(
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
                if key in self._bufs:
                    self._bufs[key].data[idx] = bytes(msg.payload[6:])

            elif action == "end":
                meta   = json.loads(msg.payload)
                img_id = int(meta["id"])
                key    = (prefix, img_id)
                buf    = self._bufs.pop(key, None)
                if buf is None or not meta.get("ok", 0) or not buf.complete():
                    return
                self._q.put(("frame", prefix, buf.assemble(), buf.dark))
                # Neteja buffers antics (> 60 s)
                stale = [k for k, b in self._bufs.items()
                         if time.monotonic() - b.born > 60]
                for k in stale:
                    del self._bufs[k]

        except Exception:
            pass  # missatges malformats ignorats

    def _mqtt_thread(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="camsec-desktop-viewer",
                             protocol=mqtt.MQTTv311,
                             clean_session=True)
        if self.args.user:
            client.username_pw_set(self.args.user, self.args.password or None)
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message
        while True:
            try:
                client.connect(self.args.broker, self.args.port, keepalive=60)
                client.loop_forever()
            except Exception as exc:
                self._q.put(("status", f"Error: {exc} \u2013 reintentant en 10 s",
                             YELLOW))
                time.sleep(10)


# ─── Entrada ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="CamSec \u2013 Visor d'escriptori de les c\u00e0meres ESP32-CAM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Els valors per defecte es llegeixen del fitxer .env (si existeix)\n"
            "o de les variables d'entorn: MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS.\n\n"
            "Exemple:\n"
            "  cp tools/.env.example tools/.env   # edita amb la teva IP i credencials\n"
            "  python cam_viewer.py"
        ),
    )
    ap.add_argument("--broker",   default=os.getenv("MQTT_BROKER", "localhost"),
                    metavar="IP",
                    help="IP o hostname del broker MQTT (per defecte: MQTT_BROKER o localhost)")
    ap.add_argument("--port",     type=int, default=int(os.getenv("MQTT_PORT", "1883")),
                    metavar="N",
                    help="Port TCP del broker MQTT (per defecte: MQTT_PORT o 1883)")
    ap.add_argument("--user",     default=os.getenv("MQTT_USER", ""),
                    metavar="USUARI",
                    help="Usuari MQTT; ometre si el broker és anònim (per defecte: MQTT_USER)")
    ap.add_argument("--password", default=os.getenv("MQTT_PASS", ""),
                    metavar="CLAU",
                    help="Contrasenya MQTT (per defecte: MQTT_PASS)")
    args = ap.parse_args()

    root = tk.Tk()
    root.resizable(True, True)
    ViewerApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
