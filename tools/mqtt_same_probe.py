#!/usr/bin/env python3
"""
CamSec MQTT Same Probe
======================
Quick diagnostic tool to measure camera dedup behavior in real time.

What it tracks per camera prefix:
- full_frames: frames transmitted with image begin/data/end
- same_exact: image/same notifications with mode=exact
- same_quasi: image/same notifications with mode=quasi
- same_other: image/same notifications with any other mode
- same_ratio: percentage of deduplicated frames over total signaled frames
- detector_same_alerts: detector warnings from <prefix>/alert/same

Usage examples:
  python mqtt_same_probe.py --broker 192.168.1.10 --duration 120
  python mqtt_same_probe.py --broker 192.168.1.10 --prefix cam/01
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict

import paho.mqtt.client as mqtt


DEFAULT_BROKER = os.getenv("MQTT_BROKER", "localhost")
DEFAULT_PORT = int(os.getenv("MQTT_PORT", "1883"))
DEFAULT_USER = os.getenv("MQTT_USER", "")
DEFAULT_PASS = os.getenv("MQTT_PASS", "")
DEFAULT_CLIENT = f"camsec-same-probe-{int(time.time()) % 100000}"


def load_dotenv(dotenv_path: Path):
    """Load KEY=VALUE pairs from a .env file into process environment."""
    if not dotenv_path.exists():
        return

    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Auto-load local tools/.env (or current working dir .env) when available.
load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path.cwd() / ".env")


@dataclass
class CameraStats:
    full_frames: int = 0
    same_exact: int = 0
    same_quasi: int = 0
    same_other: int = 0
    same_last_score: int = -1
    detector_same_alerts: int = 0
    last_image_id: int = 0
    last_same_as: int = 0
    last_event_ts: float = 0.0

    def same_total(self) -> int:
        return self.same_exact + self.same_quasi + self.same_other

    def signaled_total(self) -> int:
        return self.full_frames + self.same_total()

    def same_ratio(self) -> float:
        total = self.signaled_total()
        if total <= 0:
            return 0.0
        return (self.same_total() * 100.0) / total


class Probe:
    def __init__(self, prefix: str):
        self.prefix_filter = prefix.rstrip("/") if prefix else ""
        self.by_camera: Dict[str, CameraStats] = {}
        self._pending_full = set()  # (prefix, image_id) seen at begin and pending end

    def _camera(self, prefix: str) -> CameraStats:
        if prefix not in self.by_camera:
            self.by_camera[prefix] = CameraStats()
        return self.by_camera[prefix]

    def _accept(self, prefix: str) -> bool:
        return (not self.prefix_filter) or prefix == self.prefix_filter

    def on_message(self, topic: str, payload: bytes):
        parts = topic.split("/")
        if len(parts) < 3:
            return

        # Camera stream topics: <prefix>/image/<action>
        if parts[-2] == "image":
            prefix = "/".join(parts[:-2])
            if not self._accept(prefix):
                return

            action = parts[-1]
            if action == "begin":
                try:
                    meta = json.loads(payload)
                    image_id = int(meta.get("id", 0))
                    self._pending_full.add((prefix, image_id))
                except Exception:
                    return

            elif action == "end":
                try:
                    meta = json.loads(payload)
                    image_id = int(meta.get("id", 0))
                    ok = int(meta.get("ok", 0))
                except Exception:
                    return
                key = (prefix, image_id)
                if key in self._pending_full:
                    self._pending_full.discard(key)
                    if ok:
                        st = self._camera(prefix)
                        st.full_frames += 1
                        st.last_image_id = image_id
                        st.last_event_ts = time.time()

            elif action == "same":
                try:
                    meta = json.loads(payload)
                except Exception:
                    return
                st = self._camera(prefix)
                mode = str(meta.get("mode", "exact")).lower()
                if mode == "exact":
                    st.same_exact += 1
                elif mode == "quasi":
                    st.same_quasi += 1
                else:
                    st.same_other += 1

                st.last_image_id = int(meta.get("id", 0))
                st.last_same_as = int(meta.get("same_as", 0))
                if "score" in meta:
                    try:
                        st.same_last_score = int(meta["score"])
                    except Exception:
                        pass
                st.last_event_ts = time.time()

        # Detector same-streak warning: <prefix>/alert/same
        elif len(parts) >= 3 and parts[-2] == "alert" and parts[-1] == "same":
            prefix = "/".join(parts[:-2])
            if not self._accept(prefix):
                return
            st = self._camera(prefix)
            st.detector_same_alerts += 1
            st.last_event_ts = time.time()

    def summary_lines(self):
        lines = []
        if not self.by_camera:
            return ["No camera data yet..."]

        for prefix in sorted(self.by_camera.keys()):
            st = self.by_camera[prefix]
            lines.append(
                (
                    f"{prefix:12s} | full={st.full_frames:4d} "
                    f"same(exact/quasi/other)={st.same_exact:4d}/{st.same_quasi:4d}/{st.same_other:2d} "
                    f"ratio={st.same_ratio():6.2f}% alerts_same={st.detector_same_alerts:3d} "
                    f"last_id={st.last_image_id:6d} same_as={st.last_same_as:6d} score={st.same_last_score:4d}"
                )
            )
        return lines


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CamSec MQTT same/exact/quasi probe")
    p.add_argument("--broker", default=DEFAULT_BROKER, help="MQTT broker host")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="MQTT broker port")
    p.add_argument("--user", default=DEFAULT_USER, help="MQTT username")
    p.add_argument("--password", default=DEFAULT_PASS, help="MQTT password")
    p.add_argument("--client-id", default=DEFAULT_CLIENT, help="MQTT client id")
    p.add_argument("--prefix", default="", help="Only track one camera prefix, e.g. cam/01")
    p.add_argument("--duration", type=int, default=120, help="Capture duration in seconds (0 = infinite)")
    p.add_argument("--print-every", type=int, default=10, help="Seconds between summaries")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    probe = Probe(prefix=args.prefix)
    stop = False

    def _handle_stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    client = mqtt.Client(client_id=args.client_id, protocol=mqtt.MQTTv311, clean_session=True)
    if args.user:
        client.username_pw_set(args.user, args.password or None)

    def on_connect(c, userdata, flags, rc):
        if rc != 0:
            print(f"MQTT connect failed rc={rc}")
            return
        print(f"Connected to MQTT {args.broker}:{args.port}")
        if args.prefix:
            base = args.prefix.rstrip("/")
            c.subscribe(f"{base}/image/begin", qos=0)
            c.subscribe(f"{base}/image/end", qos=0)
            c.subscribe(f"{base}/image/same", qos=0)
            c.subscribe(f"{base}/alert/same", qos=0)
        else:
            c.subscribe("+/image/begin", qos=0)
            c.subscribe("+/+/image/begin", qos=0)
            c.subscribe("+/image/end", qos=0)
            c.subscribe("+/+/image/end", qos=0)
            c.subscribe("+/image/same", qos=0)
            c.subscribe("+/+/image/same", qos=0)
            c.subscribe("+/alert/same", qos=0)
            c.subscribe("+/+/alert/same", qos=0)

    def on_message(c, userdata, msg):
        probe.on_message(msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.broker, args.port, keepalive=60)
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")
        return 2

    client.loop_start()

    started = time.time()
    last_print = 0.0
    print("Running probe... Press Ctrl+C to stop.")

    try:
        while not stop:
            now = time.time()
            if args.duration > 0 and now - started >= args.duration:
                break
            if now - last_print >= max(1, args.print_every):
                elapsed = int(now - started)
                print(f"\n=== Summary @ {elapsed}s ===")
                for line in probe.summary_lines():
                    print(line)
                last_print = now
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()

    print("\n=== Final summary ===")
    for line in probe.summary_lines():
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
