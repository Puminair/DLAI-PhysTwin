"""Layer 3 — ingest: normalise Layer-2 batches into a single record shape.

May import: stdlib, dlai.*. NEVER world/.

The two scanning streams use opposite RSSI sign conventions
(DevicesSeen positive, BluetoothDevicesSeen negative). Everything after
ingest works in dBm (negative); each record carries a `normalisation`
note recording exactly what was done — provenance on every derived
value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SensorSite:
    """Deployment knowledge: where a sensor was installed (site survey)."""
    ap_mac: str
    sensor_id: str
    x: float
    y: float
    z: float
    zone: str


def load_sensor_sites(path: str | Path) -> dict[str, SensorSite]:
    """Sensor positions from the deployment artifact, keyed by AP MAC.

    MAC scheme mirrors sensing.sensor.load_sensors — both derive from
    the same published install document, which is why Layer 3 may know
    it without seeing Layer 1.
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for s in doc["sensors"]:
        n = int(s["sensor_id"][1:])
        mac = f"00:2a:10:{(n >> 8) & 0xFF:02x}:{n & 0xFF:02x}:00"
        out[mac] = SensorSite(ap_mac=mac, sensor_id=s["sensor_id"],
                              x=s["x"], y=s["y"], z=s["z"], zone=s["zone"])
    return out


@dataclass
class NormalisedObservation:
    mac: str
    seen_time_ms: int
    delivered_at_ms: int
    rssi_dbm: list[tuple[str, float]]        # (apMac, dBm negative)
    cloud_location: dict | None              # confidence: inferred, or None
    gaps: list[str]
    manufacturer: str | None = None
    ssid: str | None = None
    normalisation: list[str] = field(default_factory=list)


def normalise_batch(batch: dict) -> list[NormalisedObservation]:
    """Normalise one delivered batch (ObservationBatch.to_json() shape)."""
    out: list[NormalisedObservation] = []
    delivered = batch["deliveredAt"]
    for rec in batch["records"]:
        stream = rec.get("stream", "")
        if not stream.startswith("scanning_api_v3"):
            continue  # air_marshal / awips flow to dlai.attack, not here
        notes = []
        rssi = []
        for r in rec.get("rssiRecords", []):
            v = float(r["rssi"])
            if "DevicesSeen" in stream and "Bluetooth" not in stream:
                v = -abs(v)
                note = "rssi_sign_flipped:DevicesSeen_positive_to_dbm"
            else:
                v = -abs(v)   # already negative; abs-neg is idempotent
                note = "rssi_passthrough:BluetoothDevicesSeen_negative"
            if note not in notes:
                notes.append(note)
            rssi.append((r["apMac"], v))
        loc = rec["locations"][0] if rec.get("locations") else None
        out.append(NormalisedObservation(
            mac=rec["clientMac"], seen_time_ms=rec["seenTime"],
            delivered_at_ms=delivered, rssi_dbm=rssi, cloud_location=loc,
            gaps=list(rec.get("gap", [])),
            manufacturer=rec.get("manufacturer"), ssid=rec.get("ssid"),
            normalisation=notes))
    return out


def split_security_records(batch: dict) -> list[dict]:
    """Air Marshal and aWIPS records, routed to the attack module."""
    return [r for r in batch["records"]
            if r.get("stream") in ("air_marshal", "awips_syslog")]
