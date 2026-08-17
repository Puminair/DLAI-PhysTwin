"""Live view server — Layer 3 inference over a real Layer-2 stream.

Layer: visualisation of DLAI output. May import config + dlai + the
published scene artifact. It MUST NOT import world/ or sensing/: the live
branch has NO ground truth. Everything it renders is an INFERENCE from
observations — resolved positions with variance, never true positions.
That is the difference from viz/server.py (the simulation view), and it
is what makes the picture honest.

Input: a JSONL stream of Layer-2 observation batches
({"deliveredAt": ms, "records": [...]}) — either
  - the sink written by cisco/scanning_receiver.py from real Meraki
    Scanning API v3 webhooks (a live deployment), or
  - a capture from scripts/capture_observations.py (for demonstration).
Batches are replayed paced by their own deliveredAt timestamps; new lines
appended to the file are picked up live (tail).

Pipeline: normalise_batch -> EntityResolver (position by elimination,
variance carried) -> interaction/policy/attack + AlertEngine. The
websocket streams resolved tracks, their heard-sensor links, and alerts.

Usage: python -m viz.live_server [--source data/capture_layer2.jsonl]
                                  [--port 8788] [--speed 8]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path

from config import load_config
from dlai.floorplan import FloorPlan
from dlai.ingest import load_sensor_sites
from viz.dlai_runtime import INJECTABLE, DlaiRuntime

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class LiveServer:
    def __init__(self, source: Path, band: str = "2.4", speed: float = 8.0):
        self.cfg = load_config()
        self.sites = load_sensor_sites(DATA / "sensing_layer2.json")
        self.plan = FloorPlan(DATA / "store_layer1.geojson")
        self.dlai = DlaiRuntime(self.sites, self.plan,
                                self.cfg.get("rules.min_aps_for_position"))
        self.source = source
        self.speed = speed
        self.band = band
        self.clients: set[asyncio.StreamWriter] = set()
        self._scene_cache: bytes | None = None

    # -- thin accessors over the shared DLAI runtime -------------------
    INJECTABLE = INJECTABLE

    @property
    def tracks(self):
        return self.dlai.tracks

    @property
    def recent_alerts(self):
        return self.dlai.recent_alerts

    @property
    def stats(self):
        return {**self.dlai.stats, "source": str(self.source), "live": True}

    def _ingest_batch(self, batch: dict) -> None:
        self.dlai.ingest_batch(batch)

    def inject(self, attack_id: str, t_ms: int) -> int:
        return self.dlai.inject(attack_id, t_ms)

    # -- static scene (floor plan artifact — deployment knowledge) -----
    def scene_payload(self) -> bytes:
        if self._scene_cache is None:
            with open(DATA / "store_scene3d.json", "r", encoding="utf-8") as fh:
                scene = json.load(fh)
            self._scene_cache = json.dumps({
                "scene": scene,
                "panel_z_m": self.cfg.get("heights.cart_panel_z_m"),
                "min_aps": self.cfg.get("rules.min_aps_for_position"),
                "mode": "LIVE — Layer 3 inference, no ground truth",
                "injectable": [{"id": k, "label": v}
                               for k, v in self.INJECTABLE.items()],
            }).encode()
        return self._scene_cache

    # -- replay / tail the source, paced by deliveredAt ----------------
    async def replay(self):
        base_wall = None
        base_ms = None
        pos = 0
        while True:
            lines = []
            if self.source.exists():
                with open(self.source, "r", encoding="utf-8") as fh:
                    fh.seek(pos)
                    for line in fh:
                        if line.strip():
                            lines.append(line)
                    pos = fh.tell()
            for line in lines:
                batch = json.loads(line)
                t_ms = batch.get("deliveredAt", 0)
                if base_ms is None:
                    base_ms, base_wall = t_ms, asyncio.get_event_loop().time()
                # pace to the batch's own clock, compressed by `speed`
                target = base_wall + (t_ms - base_ms) / 1000.0 / self.speed
                delay = target - asyncio.get_event_loop().time()
                if delay > 0:
                    await asyncio.sleep(min(delay, 3.0))
                self._ingest_batch(batch)
            await asyncio.sleep(0.5)   # tail: wait for more appended lines

    async def stream_state(self):
        while True:
            frame = json.dumps({
                "tracks": list(self.tracks.values()),
                "alerts": self.recent_alerts,
                "incidents": self.dlai.incidents,
                "stats": self.stats,
                "band": self.band,
            })
            dead = []
            for w in self.clients:
                try:
                    w.write(ws_text_frame(frame))
                    await w.drain()
                except (ConnectionError, RuntimeError):
                    dead.append(w)
            for w in dead:
                self.clients.discard(w)
            await asyncio.sleep(0.2)   # 5 Hz — inference view, not animation

    # -- HTTP + websocket ----------------------------------------------
    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        head = request.decode("latin-1")
        line = head.split("\r\n")[0]
        path = line.split(" ")[1] if len(line.split(" ")) > 1 else "/"
        headers = {}
        for h in head.split("\r\n")[1:]:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if path == "/ws" and "sec-websocket-key" in headers:
            accept = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + WS_GUID).encode()).digest()
            ).decode()
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            await writer.drain()
            self.clients.add(writer)
            try:
                while True:
                    hdr = await reader.readexactly(2)
                    ln = hdr[1] & 0x7F
                    if ln == 126:
                        ln = struct.unpack(">H", await reader.readexactly(2))[0]
                    elif ln == 127:
                        ln = struct.unpack(">Q", await reader.readexactly(8))[0]
                    if hdr[1] & 0x80:
                        await reader.readexactly(4)
                    if ln:
                        await reader.readexactly(ln)
                    if hdr[0] & 0x0F == 0x8:
                        break
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                self.clients.discard(writer)
                writer.close()
            return

        if path.startswith("/inject"):
            qs = path.split("?", 1)[1] if "?" in path else ""
            attack = dict(p.split("=", 1) for p in qs.split("&") if "=" in p
                          ).get("attack", "")
            fired = self.inject(attack, self.dlai.last_t_ms + 2000) \
                if attack in self.INJECTABLE else 0
            body = json.dumps({"attack": attack, "alerts_fired": fired}).encode()
            ctype = "application/json"
        elif path == "/scene.json":
            body, ctype = self.scene_payload(), "application/json"
        else:
            body = (Path(__file__).parent / "live.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {len(body)}\r\n"
                      "Cache-Control: no-store\r\n\r\n").encode() + body)
        await writer.drain()
        writer.close()


def ws_text_frame(text: str) -> bytes:
    payload = text.encode()
    n = len(payload)
    if n < 126:
        return bytes([0x81, n]) + payload
    if n < (1 << 16):
        return bytes([0x81, 126]) + struct.pack(">H", n) + payload
    return bytes([0x81, 127]) + struct.pack(">Q", n) + payload


async def amain(port: int, source: Path, speed: float):
    ls = LiveServer(source=source, speed=speed)
    server = await asyncio.start_server(ls.handle, "0.0.0.0", port)
    print(f"LIVE view at http://localhost:{port}/  (source: {source.name}, "
          f"Layer-3 inference only)")
    async with server:
        await asyncio.gather(server.serve_forever(), ls.replay(),
                             ls.stream_state())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--source", default=str(DATA / "capture_layer2.jsonl"),
                    help="Layer-2 JSONL: a scanning_receiver sink or a capture")
    ap.add_argument("--speed", type=float, default=8.0,
                    help="replay speed multiplier over the batch clock")
    args = ap.parse_args()
    asyncio.run(amain(args.port, Path(args.source), args.speed))


if __name__ == "__main__":
    main()
