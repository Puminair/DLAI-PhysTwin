"""Layer 2 — AirEye-style security-stream emulator: Air Marshal + aWIPS.

May import: stdlib, config, sensing.observation.

Emits the security observation streams for scripted scenarios: the five
neighbour networks that are always there, an optional rogue-on-wire,
and throttled aWIPS signatures. Faithful to the gaps: aWIPS carries no
client identity, throttling loses intensity, containment degrades RTLS.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from sensing.observation import air_marshal_record, awips_syslog_record

NEIGHBOUR_SSIDS = [
    ("HaMashbir-Guest", "b8:27:eb"), ("CafeGreg-WiFi", "60:38:e0"),
    ("Terminal-X-POS", "f4:f5:e8"), ("SuperPharm-Ops", "18:e8:29"),
    ("Centro-Facilities", "ac:84:c6"),
]


@dataclass
class AirEyeEmulator:
    seed: int = 5
    awips_throttle_interval_ms: int = 60_000
    rng: random.Random = field(init=False)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self._last_awips: dict[tuple[str, str], int] = {}
        self._suppressed: dict[tuple[str, str], int] = {}

    def neighbour_records(self, t_ms: int) -> list[dict]:
        """The five neighbour networks on this floor and above it."""
        out = []
        for i, (ssid, oui) in enumerate(NEIGHBOUR_SSIDS):
            out.append(air_marshal_record(
                bssid=f"{oui}:00:00:{i:02x}", ssid=ssid,
                channel=self.rng.choice([1, 6, 11, 36, 44]),
                first_seen_ms=t_ms - 3_600_000, last_seen_ms=t_ms,
                wired_macs=[], wired_vlans=[], manufacturer="Various",
                encryption="wpa2", contained=False, gaps=[]))
        return out

    def rogue_on_wire_record(self, t_ms: int, contained: bool = False) -> dict:
        """A rogue AP bridged onto the store VLAN — the real threat shape."""
        gaps = ["containment_rtls_degraded"] if contained else []
        return air_marshal_record(
            bssid="de:ad:be:ef:00:01", ssid="centro-ops",
            channel=6, first_seen_ms=t_ms - 120_000, last_seen_ms=t_ms,
            wired_macs=["00:50:56:aa:bb:cc"], wired_vlans=[12],
            manufacturer="Espressif", encryption="open",
            contained=contained, gaps=gaps)

    def awips_event(self, signature: str, ap_mac: str, t_ms: int) -> dict | None:
        """Throttled: one message per signature per AP per interval.

        Returns None when suppressed — the caller sees nothing, exactly
        like the real stream. Attack intensity is lost by design.
        """
        key = (signature, ap_mac)
        last = self._last_awips.get(key)
        if last is not None and t_ms - last < self.awips_throttle_interval_ms:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        suppressed = self._suppressed.pop(key, 0)
        self._last_awips[key] = t_ms
        return awips_syslog_record(signature=signature, ap_mac=ap_mac,
                                   t_ms=t_ms, suppressed_count=suppressed)
