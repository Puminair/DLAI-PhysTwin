"""Layer 3 — contextual alert engine. RECOMMEND_ONLY.

May import: stdlib, yaml (catalog parsing), dlai.*. NEVER world/,
sensing/ or eval/.

Turns attack assessments, policy recommendations and normalised
observations into alerts from the contextual catalog in
``config/alerts.yaml`` — alerts defined by THIS building's context, not
generic IDS noise. The catalog carries the thresholds (with provenance
comments); this module carries only the mechanics. Every alert names its
evidence, its confidence ("observed" only for directly-heard facts) and
its blind spots — what the alert can NOT know: intensity lost to aWIPS
throttling, inside/outside indiscriminable by RF, single fixes
untrusted under flicker. Downlink RSSI anomalies are deliberately NOT
alertable: RRM moves AP Tx power 2-26 dBm every 30 min, so they are
expected behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import dist
from pathlib import Path

import yaml

from dlai.attack import AttackAssessment
from dlai.ingest import NormalisedObservation
from dlai.policy import Recommendation

MODE = "RECOMMEND_ONLY"

# sensing/observation.py GAP_CONTAINMENT_DEGRADED — matched by string
# value, never by import: Layer 3 consumes record shapes, not L2 code.
_GAP_CONTAINMENT = "containment_rtls_degraded"

_DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "config" / "alerts.yaml"


@dataclass(frozen=True)
class Alert:
    t_ms: int
    alert_id: str
    severity: str            # "info" | "warn" | "high"
    subject: str
    action: str              # always a recommendation, never an enforcement
    evidence: dict
    confidence: str          # "observed" | "inferred"
    mode: str = MODE
    blind_spots: list[str] = field(default_factory=list)


class AlertEngine:
    """Evaluates the contextual alert catalog over Layer-3 inputs.

    ``consume_*`` methods stage candidates and update window state;
    ``evaluate()`` runs the window rules (sensor_silent, probe_surge),
    applies per-(alert_id, subject) dedup and returns the newly emitted
    alerts. All thresholds come from the catalog and stay overridable.
    """

    def __init__(self, catalog_path: str | Path | None = None,
                 dedup_window_ms: int | None = None):
        path = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        self.meta: dict = doc.get("meta", {})
        self.catalog: dict[str, dict] = {e["id"]: e for e in doc["alerts"]}
        self.dedup_window_ms: int = (
            dedup_window_ms if dedup_window_ms is not None
            else int(self.meta.get("dedup_window_ms", 300_000)))

        self.alerts: list[Alert] = []
        self._pending: list[Alert] = []
        self._last_emitted: dict[tuple[str, str], int] = {}
        self._now_ms: int = 0
        # window state
        self._sensor_last_seen: dict[str, int] = {}
        self._last_cart_fix: dict[str, tuple[int, float, float]] = {}
        self._probe_sightings: list[tuple[int, str]] = []
        # sustained impossible-motion bookkeeping, per cart MAC
        self._cart_motion_last: dict[str, tuple[int, float, float]] = {}
        self._cart_motion_streak: dict[str, int] = {}

    # ------------------------------------------------------------------ inputs

    def consume_assessment(self, a: AttackAssessment) -> None:
        """Escalate dlai.attack assessments into catalogued alerts."""
        self._advance(a.t_ms)
        if a.kind == "rogue_ap_on_wire":
            self._stage("rogue_ap_on_wire", a.t_ms, a.subject,
                        dict(a.evidence), confidence=a.confidence)
            pos_vlans = set(self._cond("rogue_ap_on_pos_vlan", "pos_vlans", []))
            vlans = set(a.evidence.get("wiredVlans") or [])
            if vlans & pos_vlans:
                ev = dict(a.evidence)
                ev["pos_vlans_matched"] = sorted(vlans & pos_vlans)
                self._stage("rogue_ap_on_pos_vlan", a.t_ms, a.subject, ev,
                            confidence=a.confidence)
        elif a.kind == "honeypot_ssid":
            # warn, never high: without wiredMacs this is most likely one
            # of the five neighbour networks on this floor or above it.
            self._stage("evil_twin_ops_ssid", a.t_ms, a.subject,
                        dict(a.evidence), confidence="inferred")
        elif a.kind == "neighbour_network":
            self._stage("neighbour_network_catalogued", a.t_ms, a.subject,
                        dict(a.evidence), confidence="inferred")
        elif a.kind == "awips_deauth_flood":
            # Evidence deliberately carries NO frame count: throttling to
            # one message per signature per AP per interval destroys
            # intensity, and the alert must say so rather than estimate.
            self._stage("deauth_flood", a.t_ms, a.subject,
                        {"signature": "deauth_flood", "apMac": a.subject,
                         "gap": a.evidence.get("gap", []),
                         "note": "intensity unknown — aWIPS throttled; "
                                 "no client identity in this stream"},
                        confidence=a.confidence)
        if (a.evidence.get("contained") is True
                or _GAP_CONTAINMENT in (a.evidence.get("gap") or [])):
            self._stage("containment_active_rtls_risk", a.t_ms, a.subject,
                        {"gap": [_GAP_CONTAINMENT], "source": "air_marshal"},
                        confidence="observed")

    def consume_recommendation(self, rec: Recommendation) -> None:
        """Pass-through escalation of dlai.policy recommendations."""
        self._advance(rec.t_ms)
        source_rule = self._cond("exit_without_checkout",
                                 "source_policy_rule",
                                 "exit_without_checkout_presence")
        if rec.rule == source_rule:
            ev = dict(rec.evidence)
            ev["source_policy_rule"] = rec.rule
            self._stage("exit_without_checkout", rec.t_ms, rec.subject_mac,
                        ev, confidence="inferred")

    def consume_observation(self, obs: NormalisedObservation) -> None:
        """Window bookkeeping + observation-driven rules."""
        t = obs.seen_time_ms
        self._advance(t)

        for ap_mac, _rssi in obs.rssi_dbm:
            prev = self._sensor_last_seen.get(ap_mac, 0)
            self._sensor_last_seen[ap_mac] = max(prev, t)

        if _GAP_CONTAINMENT in obs.gaps:
            self._stage("containment_active_rtls_risk", t, obs.mac,
                        {"gap": [_GAP_CONTAINMENT], "source": "scanning_api"},
                        confidence="observed")

        self._check_cart_duplicate(obs)
        self._check_cart_impossible_motion(obs)
        self._check_probe_surge(obs)

    # ------------------------------------------------------------------ rules

    def _check_cart_duplicate(self, obs: NormalisedObservation) -> None:
        ouis = [p.lower() for p in
                self._cond("cart_mac_duplicate", "cart_oui_prefixes", [])]
        if ouis and not any(obs.mac.lower().startswith(p) for p in ouis):
            return
        loc = obs.cloud_location
        if loc is None or "x" not in loc or "y" not in loc:
            return
        t, x, y = obs.seen_time_ms, float(loc["x"]), float(loc["y"])
        prev = self._last_cart_fix.get(obs.mac)
        self._last_cart_fix[obs.mac] = (t, x, y)
        if prev is None:
            return
        pt, px, py = prev
        dt_s = abs(t - pt) / 1000.0
        d_m = dist((x, y), (px, py))
        window_s = float(self._cond("cart_mac_duplicate", "window_s", 5.0))
        disp_m = float(self._cond("cart_mac_duplicate", "displacement_m", 30.0))
        if dt_s < window_s and d_m > disp_m:
            self._stage("cart_mac_duplicate", t, obs.mac, {
                "clientMac": obs.mac,
                "fix_a": {"t_ms": pt, "x": px, "y": py},
                "fix_b": {"t_ms": t, "x": x, "y": y},
                "displacement_m": round(d_m, 2),
                "dt_s": round(dt_s, 3),
                "note": "both fixes are cloud inferences; cloning vs gross "
                        "RTLS error is not decidable from one pair",
            }, confidence="inferred")

    def _check_cart_impossible_motion(self, obs: NormalisedObservation) -> None:
        ouis = [p.lower() for p in
                self._cond("cart_impossible_motion", "cart_oui_prefixes", [])]
        if ouis and not any(obs.mac.lower().startswith(p) for p in ouis):
            return
        loc = obs.cloud_location
        if loc is None or "x" not in loc or "y" not in loc:
            return
        t, x, y = obs.seen_time_ms, float(loc["x"]), float(loc["y"])
        prev = self._cart_motion_last.get(obs.mac)
        self._cart_motion_last[obs.mac] = (t, x, y)
        if prev is None:
            return
        pt, px, py = prev
        dt_s = (t - pt) / 1000.0
        if dt_s <= 0:
            return
        speed = dist((x, y), (px, py)) / dt_s
        max_speed = float(self._cond("cart_impossible_motion", "max_speed_ms", 1.4))
        # sustained_fixes consecutive over-speed segments — a single fast fix
        # under position flicker is untrustworthy, so we require a run.
        need = int(self._cond("cart_impossible_motion", "sustained_fixes", 3))
        if speed > max_speed:
            streak = self._cart_motion_streak.get(obs.mac, 0) + 1
            self._cart_motion_streak[obs.mac] = streak
            if streak >= need:
                self._stage("cart_impossible_motion", t, obs.mac, {
                    "clientMac": obs.mac,
                    "fixes": [{"t_ms": pt, "x": px, "y": py},
                              {"t_ms": t, "x": x, "y": y}],
                    "speeds_ms": [round(speed, 2)],
                    "sustained_over_speed_segments": streak,
                    "max_speed_ms": max_speed,
                    "note": "check the flicker / low-trust track state before "
                            "dispatch — zigzags mimic impossible motion",
                }, confidence="inferred")
                self._cart_motion_streak[obs.mac] = 0
        else:
            self._cart_motion_streak[obs.mac] = 0

    def _check_probe_surge(self, obs: NormalisedObservation) -> None:
        if obs.ssid is not None:
            return  # associated device, not a probe
        window_ms = int(float(self._cond("probe_surge_unassociated",
                                         "window_s", 60.0)) * 1000)
        threshold = int(self._cond("probe_surge_unassociated",
                                   "distinct_macs", 40))
        self._probe_sightings.append((obs.seen_time_ms, obs.mac))
        horizon = self._now_ms - window_ms
        self._probe_sightings = [(t, m) for t, m in self._probe_sightings
                                 if t >= horizon]
        distinct = {m for _t, m in self._probe_sightings}
        if len(distinct) >= threshold:
            self._stage("probe_surge_unassociated", self._now_ms,
                        "unassociated_aggregate", {
                            "distinct_mac_count": len(distinct),
                            "window_s": window_ms / 1000.0,
                            "note": "lower bound only — unassociated "
                                    "randomised MACs never reach the API",
                        }, confidence="inferred")

    def _check_sensor_silent(self) -> None:
        window_ms = int(float(self._cond("sensor_silent",
                                         "silent_window_s", 600.0)) * 1000)
        for ap_mac, last in self._sensor_last_seen.items():
            silent = self._now_ms - last
            if silent >= window_ms:
                # absence is an inference: jittered POSTs and record loss
                # mean silence can be a delivery gap, not a failure.
                self._stage("sensor_silent", self._now_ms, ap_mac, {
                    "apMac": ap_mac,
                    "last_seen_ms": last,
                    "silent_for_ms": silent,
                }, confidence="inferred")

    # ------------------------------------------------------------------ output

    def evaluate(self) -> list[Alert]:
        """Run window rules, dedup, emit. Returns only the NEW alerts."""
        self._check_sensor_silent()
        emitted: list[Alert] = []
        for alert in self._pending:
            key = (alert.alert_id, alert.subject)
            last = self._last_emitted.get(key)
            if last is not None and alert.t_ms - last < self.dedup_window_ms:
                continue
            self._last_emitted[key] = alert.t_ms
            emitted.append(alert)
        self._pending.clear()
        self.alerts.extend(emitted)
        return emitted

    # ------------------------------------------------------------------ util

    def _advance(self, t_ms: int) -> None:
        self._now_ms = max(self._now_ms, t_ms)

    def _cond(self, alert_id: str, key: str, default):
        entry = self.catalog.get(alert_id, {})
        return entry.get("context_conditions", {}).get(key, default)

    def _stage(self, alert_id: str, t_ms: int, subject: str,
               evidence: dict, confidence: str) -> None:
        entry = self.catalog[alert_id]   # KeyError = catalog/engine drift
        self._pending.append(Alert(
            t_ms=t_ms, alert_id=alert_id,
            severity=entry["severity"], subject=subject,
            action=str(entry["action"]).strip(),
            evidence=evidence, confidence=confidence,
            blind_spots=list(entry.get("known_blind_spots", []))))
