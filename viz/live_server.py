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
from dlai.alerts import AlertEngine
from dlai.attack import AttackAnalyzer
from dlai.entity import EntityResolver
from dlai.floorplan import FloorPlan
from dlai.ingest import (load_sensor_sites, normalise_batch,
                         split_security_records)
from dlai.interaction import classify_track
from dlai.policy import PolicyEngine

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class LiveServer:
    def __init__(self, source: Path, band: str = "2.4", speed: float = 8.0):
        self.cfg = load_config()
        self.sites = load_sensor_sites(DATA / "sensing_layer2.json")
        self.plan = FloorPlan(DATA / "store_layer1.geojson")
        self.resolver = EntityResolver(
            self.sites, self.plan,
            min_aps=self.cfg.get("rules.min_aps_for_position"))
        self.policy = PolicyEngine()
        self.attack = AttackAnalyzer()
        self.alerts = AlertEngine()
        self.source = source
        self.speed = speed
        self.band = band
        self.clients: set[asyncio.StreamWriter] = set()
        self._scene_cache: bytes | None = None
        self.tracks: dict[str, dict] = {}          # mac -> latest fix payload
        self.recent_alerts: list[dict] = []
        self._last_t_ms = 1_754_600_000_000        # advanced by each batch
        self.stats = {"batches": 0, "observations": 0, "positioned": 0,
                      "no_position": 0, "source": str(source), "live": True}

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

    # -- ingest one batch through Layer 3 ------------------------------
    def _ingest_batch(self, batch: dict) -> None:
        self.stats["batches"] += 1
        self._last_t_ms = max(self._last_t_ms, batch.get("deliveredAt", 0))
        for obs in normalise_batch(batch):
            self.stats["observations"] += 1
            self.alerts.consume_observation(obs)   # cart-duplicate / probe rules
            est = self.resolver.consume(obs)
            heard = [self.sites[m].sensor_id for m, _ in obs.rssi_dbm
                     if m in self.sites]
            if est is None:
                self.stats["no_position"] += 1
                # keep the mac visible as an unlocated contact (< 3 AP)
                self.tracks[obs.mac] = {
                    "mac": obs.mac, "located": False, "n_aps": len(obs.rssi_dbm),
                    "heard": heard[:8], "confidence": "inferred",
                    "manufacturer": obs.manufacturer}
                continue
            self.stats["positioned"] += 1
            track = self.resolver.tracks[obs.mac]
            self.tracks[obs.mac] = {
                "mac": obs.mac, "located": True,
                "x": est.x, "y": est.y, "variance": est.variance_m2,
                "n_aps": est.n_aps, "heard": heard[:8],
                "flicker": track.flicker_transitions,
                "confidence": est.confidence,       # always "inferred"
                "manufacturer": obs.manufacturer, "gaps": est.gaps}

        # security streams -> attack analyzer -> alert engine
        for rec in split_security_records(batch):
            a = self.attack.consume(rec)
            if a is not None:
                self.alerts.consume_assessment(a)

        # policy over tracks (flicker / exit-without-checkout)
        for track in self.resolver.tracks.values():
            for rec in self.policy.evaluate_track(track, classify_track(track)):
                self.alerts.consume_recommendation(rec)

        for al in self.alerts.evaluate():
            self.recent_alerts.append({
                "alert_id": al.alert_id, "severity": al.severity,
                "subject": al.subject, "action": al.action,
                "confidence": al.confidence})
        self.recent_alerts = self.recent_alerts[-12:]

    # -- operator-injected attacks (the demo buttons) ------------------
    # Records are built here as plain Layer-2 dicts — the same shapes
    # sensing/ emits — so the live branch imports no world/ or sensing/.
    INJECTABLE = {
        "rogue_ap_on_wire": "Rogue AP on the wire",
        "rogue_ap_on_pos_vlan": "Rogue on the POS VLAN",
        "evil_twin_ops_ssid": "Evil twin of ops SSID",
        "deauth_flood": "Deauth flood",
        "containment_abuse": "Containment active",
        "ip_camera_as_pivot": "IP-camera pivot",
        "cart_mac_clone": "Cart MAC clone",
    }

    def _air_marshal(self, *, bssid, ssid, wired_macs, wired_vlans,
                     contained=False, gaps=(), t_ms):
        return {"stream": "air_marshal", "bssid": bssid, "ssid": ssid,
                "channel": 6, "firstSeen": t_ms - 120_000, "lastSeen": t_ms,
                "wiredMacs": list(wired_macs), "wiredVlans": list(wired_vlans),
                "manufacturer": "injected", "encryption": "open",
                "contained": contained, "confidence": "observed",
                "gap": list(gaps)}

    def _devices_seen(self, mac, x, y, t_ms):
        return {"stream": "scanning_api_v3/DevicesSeen", "clientMac": mac,
                "seenTime": t_ms, "manufacturer": "RetailPanel Ltd",
                "locations": [{"x": x, "y": y, "variance": 6.0}],
                "rssiRecords": [], "gap": []}

    def inject(self, attack_id: str, t_ms: int) -> int:
        """Inject one attack as a Layer-2 batch; return alerts fired now."""
        recs = []
        if attack_id == "rogue_ap_on_wire":
            recs = [self._air_marshal(bssid="de:ad:be:ef:00:01", ssid="centro-ops",
                    wired_macs=["00:50:56:aa:bb:cc"], wired_vlans=[99], t_ms=t_ms)]
        elif attack_id == "rogue_ap_on_pos_vlan":
            recs = [self._air_marshal(bssid="de:ad:be:ef:00:02", ssid="centro-ops",
                    wired_macs=["00:50:56:aa:bb:cd"], wired_vlans=[12], t_ms=t_ms)]
        elif attack_id == "evil_twin_ops_ssid":
            recs = [self._air_marshal(bssid="a1:b2:c3:d4:e5:f6", ssid="centro-ops",
                    wired_macs=[], wired_vlans=[], t_ms=t_ms)]
        elif attack_id == "containment_abuse":
            recs = [self._air_marshal(bssid="de:ad:be:ef:00:03", ssid="rogue-x",
                    wired_macs=[], wired_vlans=[], contained=True,
                    gaps=["containment_rtls_degraded"], t_ms=t_ms)]
        elif attack_id == "ip_camera_as_pivot":
            recs = [self._air_marshal(bssid="cc:cc:cc:00:00:01", ssid="centro-ops",
                    wired_macs=["b8:a4:4f:11:22:33"], wired_vlans=[40], t_ms=t_ms)]
        elif attack_id == "deauth_flood":
            recs = [{"stream": "awips_syslog", "signature": "deauth_flood",
                     "apMac": "00:2a:10:00:00:06", "t": t_ms,
                     "confidence": "observed",
                     "gap": ["no_client_identity", "awips_throttled"]}]
        elif attack_id == "cart_mac_clone":
            # clone a currently-located cart panel to 45 m away, same instant
            victim = next((t for t in self.tracks.values()
                           if t.get("located") and t["mac"].startswith("0c:8b:7d")),
                          None)
            mac = victim["mac"] if victim else "0c:8b:7d:00:00:99"
            x = victim["x"] if victim else 20.0
            y = victim["y"] if victim else 20.0
            recs = [self._devices_seen(mac, x, y, t_ms),
                    self._devices_seen(mac, x + 45.0, y, t_ms + 1000)]
        else:
            return 0
        before = len(self.recent_alerts)
        self._ingest_batch({"deliveredAt": t_ms, "records": recs})
        return len(self.recent_alerts) - before

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
            fired = self.inject(attack, self._last_t_ms + 2000) \
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
