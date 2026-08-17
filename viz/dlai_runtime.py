"""Shared Layer-3 runtime for both twin views.

Layer: visualisation of DLAI output. Imports config + dlai ONLY — never
world/ or sensing/. Both the simulation view (viz/server.py) and the live
view (viz/live_server.py) drive this same object, so the DLAI behaviour
and the attack-injection record shapes live in exactly one place and
cannot drift between the two views.

It consumes Layer-2 batches ({"deliveredAt": ms, "records": [...]}) — the
observation shape the sensing pipeline and the Meraki receiver emit — and
maintains the resolved tracks, the recent RECOMMEND_ONLY alerts, and the
running counters. `inject()` builds the Layer-2 record for one catalogue
attack as a plain dict (no sensing import) and runs it through the same
ingest path, so an operator button and a real observation travel an
identical route to the alert engine.
"""
from __future__ import annotations

from dlai.alerts import AlertEngine
from dlai.attack import AttackAnalyzer
from dlai.entity import EntityResolver
from dlai.ingest import normalise_batch, split_security_records
from dlai.interaction import classify_track
from dlai.orchestration import IncidentOrchestrator
from dlai.policy import PolicyEngine

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


class DlaiRuntime:
    def __init__(self, sites: dict, plan, min_aps: int):
        self.sites = sites
        self.resolver = EntityResolver(sites, plan, min_aps=min_aps)
        self.policy = PolicyEngine()
        self.attack = AttackAnalyzer()
        self.alerts = AlertEngine()
        self.orchestrator = IncidentOrchestrator()
        self.tracks: dict[str, dict] = {}
        self.recent_alerts: list[dict] = []
        self.incidents: list[dict] = []
        self.last_t_ms = 1_754_600_000_000
        self.stats = {"batches": 0, "observations": 0,
                      "positioned": 0, "no_position": 0}

    # -- ingest one Layer-2 batch through Layer 3 ----------------------
    def ingest_batch(self, batch: dict) -> None:
        self.stats["batches"] += 1
        self.last_t_ms = max(self.last_t_ms, batch.get("deliveredAt", 0))
        for obs in normalise_batch(batch):
            self.stats["observations"] += 1
            self.alerts.consume_observation(obs)
            est = self.resolver.consume(obs)
            heard = [self.sites[m].sensor_id for m, _ in obs.rssi_dbm
                     if m in self.sites]
            if est is None:
                self.stats["no_position"] += 1
                self.tracks[obs.mac] = {
                    "mac": obs.mac, "located": False, "n_aps": len(obs.rssi_dbm),
                    "heard": heard[:8], "confidence": "inferred",
                    "manufacturer": obs.manufacturer}
                continue
            self.stats["positioned"] += 1
            track = self.resolver.tracks[obs.mac]
            self.tracks[obs.mac] = {
                "mac": obs.mac, "located": True, "x": est.x, "y": est.y,
                "variance": est.variance_m2, "n_aps": est.n_aps,
                "heard": heard[:8], "flicker": track.flicker_transitions,
                "confidence": est.confidence, "manufacturer": obs.manufacturer,
                "gaps": est.gaps}

        for rec in split_security_records(batch):
            a = self.attack.consume(rec)
            if a is not None:
                self.alerts.consume_assessment(a)
        for track in self.resolver.tracks.values():
            for rec in self.policy.evaluate_track(track, classify_track(track)):
                self.alerts.consume_recommendation(rec)

        for al in self.alerts.evaluate():
            self.recent_alerts.append({
                "alert_id": al.alert_id, "severity": al.severity,
                "subject": al.subject, "action": al.action,
                "confidence": al.confidence})
            self.orchestrator.consume(al)          # correlate → incident → playbook
        self.recent_alerts = self.recent_alerts[-14:]
        self.incidents = self.orchestrator.incidents()

    # -- attack injection ---------------------------------------------
    def inject(self, attack_id: str, t_ms: int) -> int:
        """Inject one catalogue attack; return how many alerts fired now."""
        victim = next((t for t in self.tracks.values()
                       if t.get("located") and t["mac"].startswith("0c:8b:7d")),
                      None)
        kw = ({"x": victim["x"], "y": victim["y"], "mac": victim["mac"]}
              if victim else {})
        recs = attack_records(attack_id, t_ms, **kw)
        if not recs:
            return 0
        before = len(self.recent_alerts)
        self.ingest_batch({"deliveredAt": t_ms, "records": recs})
        return len(self.recent_alerts) - before
