"""Layer 2 — observation record shapes: Scanning API v3 and Air Marshal.

May import: stdlib only. Shapes are emitted exactly as specified; the
sign convention DIFFERS between DevicesSeen (positive RSSI) and
BluetoothDevicesSeen (negative). Layer 3 must normalise on ingest and
record that it did.

Every record carries `confidence` ("observed" | "inferred") and a `gap`
list naming anything withheld. An inference must never claim to be an
observation — `locations[]` is the cloud's estimate, so it is inferred;
`rssiRecords[]` are heard frames, so they are observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONFIDENCE_OBSERVED = "observed"
CONFIDENCE_INFERRED = "inferred"

GAP_NO_LOCATION = "no_location_lt_min_aps"
GAP_THROTTLED = "awips_throttled"
GAP_NO_CLIENT_IDENTITY = "no_client_identity"
GAP_CONTAINMENT_DEGRADED = "containment_rtls_degraded"
GAP_STATION_TO_STATION = "station_to_station_not_reported"


def devices_seen_record(*, client_mac: str, ipv4: str | None, ssid: str | None,
                        os_hint: str | None, manufacturer: str,
                        seen_time_ms: int, floor_plan_id: str,
                        rssi_records: list[dict],
                        location: dict | None,
                        gaps: list[str]) -> dict:
    """Scanning API v3 DevicesSeen — RSSI values are POSITIVE by convention."""
    return {
        "stream": "scanning_api_v3/DevicesSeen",
        "clientMac": client_mac,
        "ipv4": ipv4,
        "ssid": ssid,
        "os": os_hint,
        "manufacturer": manufacturer,
        "seenTime": seen_time_ms,
        "locations": [] if location is None else [{
            "x": location["x"], "y": location["y"],
            "variance": location["variance"],
            "floorPlanId": floor_plan_id,
            "confidence": CONFIDENCE_INFERRED,
        }],
        "rssiRecords": [
            # positive sign convention — Layer 3 normalises on ingest
            {"apMac": r["apMac"], "rssi": abs(r["rssi"])}
            for r in rssi_records
        ],
        "confidence": CONFIDENCE_OBSERVED,
        "gap": list(gaps),
    }


def bluetooth_devices_seen_record(*, client_mac: str, manufacturer: str,
                                  seen_time_ms: int,
                                  rssi_records: list[dict],
                                  gaps: list[str]) -> dict:
    """BluetoothDevicesSeen — RSSI values are NEGATIVE by convention."""
    return {
        "stream": "scanning_api_v3/BluetoothDevicesSeen",
        "clientMac": client_mac,
        "manufacturer": manufacturer,
        "seenTime": seen_time_ms,
        "rssiRecords": [
            {"apMac": r["apMac"], "rssi": -abs(r["rssi"])}
            for r in rssi_records
        ],
        "confidence": CONFIDENCE_OBSERVED,
        "gap": list(gaps),
    }


def air_marshal_record(*, bssid: str, ssid: str, channel: int,
                       first_seen_ms: int, last_seen_ms: int,
                       wired_macs: list[str], wired_vlans: list[int],
                       manufacturer: str, encryption: str,
                       contained: bool, gaps: list[str]) -> dict:
    """Air Marshal rogue/neighbour record.

    `wiredMacs` is the field that separates a rogue attached to your
    network from one of the five neighbour networks on this floor and
    above it. Without it the false-positive rate is unusable.
    """
    return {
        "stream": "air_marshal",
        "bssid": bssid,
        "ssid": ssid,
        "channel": channel,
        "firstSeen": first_seen_ms,
        "lastSeen": last_seen_ms,
        "wiredMacs": list(wired_macs),
        "wiredVlans": list(wired_vlans),
        "manufacturer": manufacturer,
        "encryption": encryption,
        "contained": contained,
        "confidence": CONFIDENCE_OBSERVED,
        "gap": list(gaps),
    }


def awips_syslog_record(*, signature: str, ap_mac: str, t_ms: int,
                        suppressed_count: int) -> dict:
    """aWIPS syslog — carries NO client identity at all, by design.

    Throttling: one message per signature per AP per interval, so attack
    intensity is lost; `suppressed_count` only says that suppression
    happened, not how much was suppressed (that is the point).
    """
    return {
        "stream": "awips_syslog",
        "signature": signature,
        "apMac": ap_mac,
        "t": t_ms,
        "confidence": CONFIDENCE_OBSERVED,
        "gap": [GAP_NO_CLIENT_IDENTITY] + ([GAP_THROTTLED] if suppressed_count else []),
    }


@dataclass
class ObservationBatch:
    """One POST from the cloud: records observed earlier, delivered now."""
    delivered_at_ms: int
    records: list[dict] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"deliveredAt": self.delivered_at_ms, "records": self.records}
