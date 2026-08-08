"""Visualisation server — websocket state stream at 10 Hz.

May import: stdlib, config, world, sensing, eval. The renderer holds no
simulation logic; this process runs the world and streams state.

Zero external dependencies: a minimal HTTP + WebSocket (RFC 6455)
server on asyncio. Endpoints:
  GET /            -> twin.html
  GET /scene.json  -> static scene payload (solids, sensors, coverage)
  GET /ws          -> websocket, JSON state frames at 10 Hz

Cart->sensor link lines are recomputed at 1 Hz (physics cost), streamed
with every frame. Carts with fewer than three audible sensors are the
red ones — that behaviour is the thesis of the whole build.

Usage: python -m viz.server [--port 8787] [--band 2.4]
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
from eval.coverage import coverage_grid
from sensing.sensor import SensorField, load_sensors
from world.geometry import StoreGeometry
from world.sim import WorldSim

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class TwinServer:
    def __init__(self, band: str = "2.4", sim_dt_s: float = 0.1,
                 time_scale: float = 4.0):
        self.cfg = load_config()
        self.geo = StoreGeometry(DATA / "store_layer1.geojson")
        self.field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                                 geometry=self.geo, cfg=self.cfg)
        self.sim = WorldSim(geometry=self.geo, cfg=self.cfg)
        self.band = band
        self.sim_dt_s = sim_dt_s
        self.time_scale = time_scale
        self.links: dict[str, list[dict]] = {}
        self.clients: set[asyncio.StreamWriter] = set()
        self._scene_cache: bytes | None = None

    # -- static payload ------------------------------------------------
    def scene_payload(self) -> bytes:
        if self._scene_cache is None:
            with open(DATA / "store_scene3d.json", "r", encoding="utf-8") as fh:
                scene = json.load(fh)
            cov_path = DATA / "coverage_report.json"
            if cov_path.exists():
                with open(cov_path, "r", encoding="utf-8") as fh:
                    coverage = json.load(fh)
            else:
                print(f"coverage_report.json missing; computing {self.band} "
                      "(run scripts/build_coverage.py for all bands)...")
                coverage = {self.band: coverage_grid(self.field, self.cfg,
                                                     self.band)}
            self._scene_cache = json.dumps({
                "scene": scene,
                "coverage": coverage,
                "panel_z_m": self.cfg.get("heights.cart_panel_z_m"),
                "min_aps": self.cfg.get("rules.min_aps_for_position"),
                "threshold_dbm": self.cfg.get("rules.location_threshold_dbm"),
            }).encode()
        return self._scene_cache

    # -- simulation loops ----------------------------------------------
    async def run_sim(self):
        while True:
            self.sim.step(self.sim_dt_s * self.time_scale)
            await asyncio.sleep(self.sim_dt_s)

    async def refresh_links(self):
        eirp = self.cfg.get("cart_uplink.eirp_dbm")[self.band]
        panel_z = self.cfg.get("heights.cart_panel_z_m")
        while True:
            snap = self.sim.snapshot()
            shopper_xy = [(s["x"], s["y"]) for s in snap["shoppers"]]
            links = {}
            for c in snap["carts"]:
                if c["state"] == "docked":
                    continue
                recs = self.field.hear_device(
                    band=self.band, eirp_dbm=eirp,
                    device_xyz=(c["x"], c["y"], panel_z),
                    shopper_xy=shopper_xy, use_reflections_on_fail=False)
                links[c["id"]] = [
                    {"sensor_id": r["sensor_id"], "rssi": r["rssi"]}
                    for r in recs[:8]]
                await asyncio.sleep(0)   # keep the stream loop breathing
            self.links = links
            await asyncio.sleep(1.0)

    async def stream_state(self):
        while True:
            snap = self.sim.snapshot()
            frame = json.dumps({
                "t_ms": snap["t_ms"],
                "carts": snap["carts"],
                "shoppers": snap["shoppers"],
                "staff": snap["staff"],
                "links": self.links,
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
            await asyncio.sleep(0.1)     # 10 Hz

    # -- protocol ------------------------------------------------------
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
                while True:                # read (and discard) client frames
                    hdr = await reader.readexactly(2)
                    opcode = hdr[0] & 0x0F
                    length = hdr[1] & 0x7F
                    masked = bool(hdr[1] & 0x80)
                    if length == 126:
                        length = struct.unpack(">H", await reader.readexactly(2))[0]
                    elif length == 127:
                        length = struct.unpack(">Q", await reader.readexactly(8))[0]
                    if masked:
                        await reader.readexactly(4)
                    if length:
                        await reader.readexactly(length)
                    if opcode == 0x8:      # close
                        break
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                self.clients.discard(writer)
                writer.close()
            return

        if path == "/scene.json":
            body = self.scene_payload()
            ctype = "application/json"
        else:
            body = (Path(__file__).parent / "twin.html").read_bytes()
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


async def amain(port: int, band: str):
    ts = TwinServer(band=band)
    server = await asyncio.start_server(ts.handle, "0.0.0.0", port)
    print(f"twin at http://localhost:{port}/  (band {band})")
    async with server:
        await asyncio.gather(server.serve_forever(), ts.run_sim(),
                             ts.refresh_links(), ts.stream_state())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--band", default="2.4", choices=["2.4", "5", "6"])
    args = ap.parse_args()
    asyncio.run(amain(args.port, args.band))


if __name__ == "__main__":
    main()
