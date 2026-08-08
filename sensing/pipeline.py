"""Layer 2 — the delivery pipeline: batching, jitter, loss, MAC policy.

May import: stdlib, config, world (to observe snapshots), sensing.*.

This module is where world facts become observations. The mapping from
a cart's world id to its panel's MAC lives HERE and only here — it is
physical reality (the panel is bolted to the cart) but it is never
emitted in any record. Emitted records carry MACs only; recovering the
link is Layer 3's inference problem. `truth_links()` exposes the map
for the evaluation harness alone, so the blind test can score that
inference — Layer 3 must never call it (enforced by the import ban).

Gaps modelled here, per CLAUDE.md §5:
- unassociated randomised MACs are dropped and never reach the API
- POST interval 60-180 s, jittered, not guaranteed
- station-to-station traffic is not reported by any stream
- 2% record loss
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from sensing.observation import (GAP_NO_LOCATION, ObservationBatch,
                                 devices_seen_record)
from sensing.sensor import SensorField


def _mac(prefix: str, n: int) -> str:
    return f"{prefix}:{(n >> 16) & 0xFF:02x}:{(n >> 8) & 0xFF:02x}:{n & 0xFF:02x}"


@dataclass
class DeviceRf:
    """Private RF identity of a physical device. Never emitted whole."""
    mac: str
    manufacturer: str
    randomized: bool
    associated: bool
    band: str
    eirp_dbm: float
    antenna_z_m: float


@dataclass
class ObservationPipeline:
    field_: SensorField
    cfg: "object"
    floor_plan_id: str = "centro_-1"
    seed: int = 99

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self._rf: dict[str, DeviceRf] = {}       # world id -> RF identity (private)
        self._batch: list[dict] = []
        self._next_flush_ms: int | None = None
        self._min_aps = self.cfg.get("rules.min_aps_for_position")
        self._loss = self.cfg.get("pipeline.record_loss_prob")
        self._post_lo, self._post_hi = self.cfg.get("pipeline.post_interval_s")
        self._staff_every = int(self.cfg.get("staffing.phone")["observe_every_n_ticks"])
        self._tick = 0
        self.delivered: list[ObservationBatch] = []

    # -- private identity management ----------------------------------
    def _rf_for_cart(self, cart_id: str) -> DeviceRf:
        if cart_id not in self._rf:
            n = len(self._rf) + 1
            band = "2.4"   # positioning band — 2.4 wins decisively
            eirp = self.cfg.get("cart_uplink.eirp_dbm")[band]
            self._rf[cart_id] = DeviceRf(
                mac=_mac("0c:8b:7d", n), manufacturer="RetailPanel Ltd",
                randomized=False, associated=True, band=band,
                eirp_dbm=eirp,
                antenna_z_m=self.cfg.get("heights.cart_panel_z_m"))
        return self._rf[cart_id]

    def _rf_for_staff(self, staff_id: str) -> DeviceRf:
        """Staff phones: stable MACs, associated to the ops SSID."""
        if staff_id not in self._rf:
            n = len(self._rf) + 1
            phone = self.cfg.get("staffing.phone")
            band = phone["band"]
            self._rf[staff_id] = DeviceRf(
                mac=_mac("a4:5e:60", n), manufacturer="Samsung",
                randomized=False, associated=True, band=band,
                eirp_dbm=phone["eirp_dbm"][band],
                antenna_z_m=phone["antenna_z_m"])
        return self._rf[staff_id]

    def _rf_for_shopper(self, shopper_id: str) -> DeviceRf:
        if shopper_id not in self._rf:
            n = len(self._rf) + 1
            randomized = self.rng.random() < self.cfg.get("pipeline.randomized_mac_prob")
            prefix = "da:a1:19" if randomized else "3c:22:fb"   # locally administered
            self._rf[shopper_id] = DeviceRf(
                mac=_mac(prefix, self.rng.randrange(1 << 24) if randomized else n),
                manufacturer="Unknown" if randomized else "Apple",
                randomized=randomized, associated=not randomized,
                band="2.4", eirp_dbm=14.0, antenna_z_m=1.1)
        return self._rf[shopper_id]

    def truth_links(self) -> dict[str, str]:
        """world id -> MAC. FOR eval/ ONLY — scoring the blind test.

        Layer 3 consuming this would collapse the identity separation;
        the dlai import ban is what makes calling it impossible there.
        """
        return {wid: rf.mac for wid, rf in self._rf.items()}

    # -- observation ---------------------------------------------------
    @staticmethod
    def bodies_xy(snapshot: dict) -> list[tuple[float, float]]:
        """Every human body in the world absorbs RF — shoppers AND staff."""
        return ([(s["x"], s["y"]) for s in snapshot["shoppers"]]
                + [(m["x"], m["y"]) for m in snapshot.get("staff", [])])

    def observe(self, snapshot: dict) -> None:
        """Observe one world snapshot; queue records for later delivery."""
        t_ms = snapshot["t_ms"]
        self._tick += 1
        shopper_xy = self.bodies_xy(snapshot)

        for cart in snapshot["carts"]:
            if cart["state"] == "docked":
                continue
            rf = self._rf_for_cart(cart["id"])
            records = self.field_.hear_device(
                band=rf.band, eirp_dbm=rf.eirp_dbm,
                device_xyz=(cart["x"], cart["y"], rf.antenna_z_m),
                shopper_xy=shopper_xy)
            if not records:
                continue
            self._queue_device(rf, t_ms, records)

        for shopper in snapshot["shoppers"]:
            rf = self._rf_for_shopper(shopper["id"])
            if rf.randomized and not rf.associated:
                continue   # dropped: never reaches the API — the gap is real
            records = self.field_.hear_device(
                band=rf.band, eirp_dbm=rf.eirp_dbm,
                device_xyz=(shopper["x"], shopper["y"], rf.antenna_z_m),
                shopper_xy=shopper_xy)
            if not records:
                continue
            self._queue_device(rf, t_ms, records)

        # staff phones are associated and always present; the cloud samples
        # them round-robin rather than reporting every phone every tick
        for i, member in enumerate(snapshot.get("staff", [])):
            if (self._tick + i) % self._staff_every:
                continue
            rf = self._rf_for_staff(member["id"])
            records = self.field_.hear_device(
                band=rf.band, eirp_dbm=rf.eirp_dbm,
                device_xyz=(member["x"], member["y"], rf.antenna_z_m),
                shopper_xy=shopper_xy)
            if not records:
                continue
            self._queue_device(rf, t_ms, records)

        self._maybe_flush(t_ms)

    def _queue_device(self, rf: DeviceRf, t_ms: int, records: list[dict]) -> None:
        if self.rng.random() < self._loss:
            return    # record loss — modelled, not a bug
        gaps: list[str] = []
        location = None
        if len(records) >= self._min_aps:
            location = self._cloud_location(records)
        else:
            gaps.append(GAP_NO_LOCATION)
        self._batch.append(devices_seen_record(
            client_mac=rf.mac, ipv4=None,
            ssid="centro-ops" if rf.associated else None,
            os_hint=None, manufacturer=rf.manufacturer,
            seen_time_ms=t_ms, floor_plan_id=self.floor_plan_id,
            rssi_records=records, location=location, gaps=gaps))

    def _cloud_location(self, records: list[dict]) -> dict:
        """The cloud's own coarse estimate: RSSI-weighted sensor centroid.

        Deliberately crude — it is an inference the cloud makes, marked
        as such in the record. Layer 3 may use it or do better from the
        rssiRecords; the variance carries the spread, never collapsed.
        """
        units = {u.mac: u for u in self.field_.units}
        wsum = xsum = ysum = 0.0
        pts = []
        for r in records[:6]:
            u = units[r["apMac"]]
            w = 10.0 ** (r["rssi"] / 20.0)
            wsum += w
            xsum += u.x * w
            ysum += u.y * w
            pts.append((u.x, u.y))
        x, y = xsum / wsum, ysum / wsum
        var = sum((px - x) ** 2 + (py - y) ** 2 for px, py in pts) / len(pts)
        return {"x": round(x, 2), "y": round(y, 2), "variance": round(var, 2)}

    def _maybe_flush(self, t_ms: int) -> None:
        if self._next_flush_ms is None:
            self._next_flush_ms = t_ms + int(
                self.rng.uniform(self._post_lo, self._post_hi) * 1000)
            return
        if t_ms >= self._next_flush_ms and self._batch:
            self.delivered.append(ObservationBatch(
                delivered_at_ms=t_ms, records=self._batch))
            self._batch = []
            self._next_flush_ms = t_ms + int(
                self.rng.uniform(self._post_lo, self._post_hi) * 1000)

    def flush_all(self, t_ms: int) -> None:
        """End-of-run flush so the tail of the trace is not lost."""
        if self._batch:
            self.delivered.append(ObservationBatch(
                delivered_at_ms=t_ms, records=self._batch))
            self._batch = []
