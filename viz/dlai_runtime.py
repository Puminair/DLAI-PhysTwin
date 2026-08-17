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
attack as a plain dict (via viz.attack_shapes, no sensing/dlai coupling in
the builders) and runs it through the same ingest path, so an operator
button and a real observation travel an identical route to the alert
engine.

The attack record shapes live in viz/attack_shapes.py so the physical-twin
producer can emit them without importing any Layer-3 (dlai/) module. They
are re-exported here for backward compatibility.
"""
from __future__ import annotations

from dlai.alerts import AlertEngine
from dlai.attack import AttackAnalyzer
from dlai.entity import EntityResolver
from dlai.ingest import normalise_batch, split_security_records
from dlai.interaction import classify_track
from dlai.orchestration import IncidentOrchestrator
from dlai.policy import PolicyEngine
from viz.attack_shapes import INJECTABLE, attack_records

__all__ = ["INJECTABLE", "attack_records", "DlaiRuntime"]


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
                "gaps": est.gaps, "interaction": "—"}

        for rec in split_security_records(batch):
            a = self.attack.consume(rec)
            if a is not None:
                self.alerts.consume_assessment(a)
        # interaction classification (dwell/transit/checkout/exit) — computed
        # once per track and reused for the policy engine
        for track in self.resolver.tracks.values():
            interactions = classify_track(track)
            if interactions and track.mac in self.tracks:
                self.tracks[track.mac]["interaction"] = interactions[-1].kind
            for rec in self.policy.evaluate_track(track, interactions):
                self.alerts.consume_recommendation(rec)

        for al in self.alerts.evaluate():
            self.recent_alerts.append({
                "alert_id": al.alert_id, "severity": al.severity,
                "subject": al.subject, "action": al.action,
                "confidence": al.confidence,
                "blind_spots": list(al.blind_spots or [])})
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
