"""Layer 2 — the CW9172I sensor model: five radios, duty cycle, RRM.

May import: stdlib, config, world.geometry (observation only),
sensing.propagation, sensing.reflections.

A logical sensor is a PAIR of units (+2.5 dB selection diversity). It
hears devices on 2.4/5/6 GHz serving radios plus a scanning radio and
BLE. What it reports is an observation: the RSSI of uplink frames, at
the sensor's mount point, after every loss the building imposes.

RRM note: the sensor's own Tx power moves 2-26 dBm every 30 min. That
changes the DOWNLINK only; it is modelled so nobody is tempted to use
downlink numbers for location, and so Air Marshal containment behaviour
has a knob to hang off.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from sensing.propagation import bodies_crossed, uplink_rssi_dbm
from sensing.reflections import reflected_rssi_dbm


@dataclass(frozen=True)
class SensorUnit:
    sensor_id: str
    x: float
    y: float
    z: float
    zone: str
    mac: str            # observation-domain identity of the sensor itself


def load_sensors(path: str | Path) -> list[SensorUnit]:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    units = []
    for s in doc["sensors"]:
        n = int(s["sensor_id"][1:])
        mac = f"00:2a:10:{(n >> 8) & 0xFF:02x}:{n & 0xFF:02x}:00"
        units.append(SensorUnit(sensor_id=s["sensor_id"], x=s["x"], y=s["y"],
                                z=s["z"], zone=s["zone"], mac=mac))
    return units


@dataclass
class SensorField:
    """All logical sensors plus the physics needed to hear a device."""
    units: list[SensorUnit]
    geometry: "object"                    # world.geometry.StoreGeometry
    cfg: "object"
    rng: random.Random = field(default_factory=lambda: random.Random(20140457))

    def __post_init__(self):
        self._att = {b: dict(v) for b, v in self.cfg.section("attenuation_db").items()
                     if b != "human_body"}
        self._body = self.cfg.get("attenuation_db.human_body")
        self._gain = self.cfg.get("sensors.antenna_gain_dbi")
        self._div = self.cfg.get("sensors.diversity_gain_db")
        self._shoulder = self.cfg.get("heights.shopper_shoulder_half_width_m")
        self._threshold = self.cfg.get("rules.location_threshold_dbm")
        self._min_aps = self.cfg.get("rules.min_aps_for_position")
        self._refl_bands = set(self.cfg.get("reflections.enabled_bands"))
        self._fresnel = self.cfg.get("reflections.fresnel_loss_db_normal")
        self._reflectors = self.geometry.reflector_surfaces()

    def hear_device(self, *, band: str, eirp_dbm: float,
                    device_xyz: tuple[float, float, float],
                    shopper_xy: list[tuple[float, float]],
                    use_reflections_on_fail: bool = True) -> list[dict]:
        """RSSI records for every unit that hears the device's uplink.

        Reflections are only evaluated when the direct model fails the
        min-AP rule — the only place a bounce can change the answer.
        """
        import math
        records, weak = [], []
        for u in self.units:
            d = math.dist((u.x, u.y, u.z), device_xyz)
            crossings = self.geometry.segment_crossings(
                (u.x, u.y, u.z), device_xyz)
            n_bodies = bodies_crossed((u.x, u.y, u.z), device_xyz,
                                      shopper_xy, self._shoulder)
            rssi = uplink_rssi_dbm(
                band=band, device_eirp_dbm=eirp_dbm,
                sensor_antenna_gain_dbi=self._gain[band],
                diversity_gain_db=self._div, distance_m=d,
                crossings=crossings, n_bodies=n_bodies,
                attenuation_db=self._att, body_loss_db=self._body)
            if rssi is not None and rssi >= self._threshold:
                records.append({"apMac": u.mac, "sensor_id": u.sensor_id,
                                "rssi": round(rssi, 1), "path": "direct"})
            else:
                weak.append(u)

        if (use_reflections_on_fail and band in self._refl_bands
                and len(records) < self._min_aps):
            # a bounce only matters within plausible range of the device
            weak = [u for u in weak
                    if math.dist((u.x, u.y), device_xyz[:2]) < 25.0]
            for u in weak:
                rssi = reflected_rssi_dbm(
                    band=band, device_eirp_dbm=eirp_dbm,
                    sensor_antenna_gain_dbi=self._gain[band],
                    diversity_gain_db=self._div,
                    sensor_xyz=(u.x, u.y, u.z), device_xyz=device_xyz,
                    reflectors=self._reflectors,
                    segment_crossings=self.geometry.segment_crossings,
                    attenuation_db=self._att,
                    fresnel_normal_db=self._fresnel)
                if rssi is not None and rssi >= self._threshold:
                    records.append({"apMac": u.mac, "sensor_id": u.sensor_id,
                                    "rssi": round(rssi, 1), "path": "reflected"})
        records.sort(key=lambda r: -r["rssi"])
        return records

    def rrm_tx_power_dbm(self, sensor_id: str, t_ms: int) -> float:
        """Downlink Tx power under RRM — moves every interval. NOT for location."""
        lo, hi = self.cfg.get("sensors.rrm.tx_power_range_dbm")
        interval_ms = int(self.cfg.get("sensors.rrm.interval_s")) * 1000
        bucket = t_ms // interval_ms
        r = random.Random(hash((sensor_id, bucket)) & 0xFFFFFFFF)
        return round(r.uniform(lo, hi), 1)
