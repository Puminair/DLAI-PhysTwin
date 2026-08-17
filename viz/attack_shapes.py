"""Layer-2 record shapes for operator-injectable attacks.

Layer: sensing-shape helpers. Imports stdlib ONLY — no world/, no
sensing/, no dlai/. This module is the single source of truth for what an
injected catalogue attack *looks like* on the wire/air: plain dicts in the
exact shapes the sensing pipeline and the Meraki receiver emit.

It lives apart from viz/dlai_runtime.py on purpose. The physical-twin
producer (viz/server.py) only needs to *emit* these observation dicts to
its Layer-2 stream — it must never depend on Layer-3 (dlai/). Keeping the
record builders here lets the producer import them without dragging in a
single DLAI module, so the twin process stays Layer-1/2 only. The Layer-3
runtime (DlaiRuntime) imports the same builders from here, so an operator
button and a real observation travel an identical route to the engines.
"""
from __future__ import annotations

# operator-injectable attacks (id -> button label). Records are built below
# as plain Layer-2 dicts, the shapes sensing/ emits.
INJECTABLE = {
    "rogue_ap_on_wire": "Rogue AP on the wire",
    "rogue_ap_on_pos_vlan": "Rogue on the POS VLAN",
    "evil_twin_ops_ssid": "Evil twin of ops SSID",
    "deauth_flood": "Deauth flood",
    "containment_abuse": "Containment active",
    "ip_camera_as_pivot": "IP-camera pivot",
    "cart_mac_clone": "Cart MAC clone",
}


def _air_marshal(*, bssid, ssid, wired_macs, wired_vlans,
                 contained=False, gaps=(), t_ms):
    return {"stream": "air_marshal", "bssid": bssid, "ssid": ssid,
            "channel": 6, "firstSeen": t_ms - 120_000, "lastSeen": t_ms,
            "wiredMacs": list(wired_macs), "wiredVlans": list(wired_vlans),
            "manufacturer": "injected", "encryption": "open",
            "contained": contained, "confidence": "observed", "gap": list(gaps)}


def _devices_seen(mac, x, y, t_ms):
    return {"stream": "scanning_api_v3/DevicesSeen", "clientMac": mac,
            "seenTime": t_ms, "manufacturer": "RetailPanel Ltd",
            "locations": [{"x": x, "y": y, "variance": 6.0}],
            "rssiRecords": [], "gap": []}


def attack_records(attack_id: str, t_ms: int, *,
                   x: float = 20.0, y: float = 20.0,
                   mac: str = "0c:8b:7d:00:00:99") -> list[dict]:
    """Build the Layer-2 record(s) for one catalogue attack.

    The single source of truth for what an injected attack *looks like* on
    the wire/air — used both when a view runs the DLAI itself and when the
    physical twin merely emits the attack to a stream a separate DLAI
    process consumes. `x, y, mac` locate the cart-clone's genuine victim.
    """
    if attack_id == "rogue_ap_on_wire":
        return [_air_marshal(bssid="de:ad:be:ef:00:01", ssid="centro-ops",
                wired_macs=["00:50:56:aa:bb:cc"], wired_vlans=[99], t_ms=t_ms)]
    if attack_id == "rogue_ap_on_pos_vlan":
        return [_air_marshal(bssid="de:ad:be:ef:00:02", ssid="centro-ops",
                wired_macs=["00:50:56:aa:bb:cd"], wired_vlans=[12], t_ms=t_ms)]
    if attack_id == "evil_twin_ops_ssid":
        return [_air_marshal(bssid="a1:b2:c3:d4:e5:f6", ssid="centro-ops",
                wired_macs=[], wired_vlans=[], t_ms=t_ms)]
    if attack_id == "containment_abuse":
        return [_air_marshal(bssid="de:ad:be:ef:00:03", ssid="rogue-x",
                wired_macs=[], wired_vlans=[], contained=True,
                gaps=["containment_rtls_degraded"], t_ms=t_ms)]
    if attack_id == "ip_camera_as_pivot":
        return [_air_marshal(bssid="cc:cc:cc:00:00:01", ssid="centro-ops",
                wired_macs=["b8:a4:4f:11:22:33"], wired_vlans=[40], t_ms=t_ms)]
    if attack_id == "deauth_flood":
        return [{"stream": "awips_syslog", "signature": "deauth_flood",
                 "apMac": "00:2a:10:00:00:06", "t": t_ms,
                 "confidence": "observed",
                 "gap": ["no_client_identity", "awips_throttled"]}]
    if attack_id == "cart_mac_clone":
        return [_devices_seen(mac, x, y, t_ms),
                _devices_seen(mac, x + 45.0, y, t_ms + 1000)]
    return []
