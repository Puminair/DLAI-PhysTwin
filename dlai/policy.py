"""Layer 3 — policy engine. RECOMMEND_ONLY: no automated blocking.

May import: stdlib, dlai.*. NEVER world/.

Rules consume interactions and track state and produce recommendations
with evidence attached. Every output names the observations it rests on
— when someone asks in six months why a recommendation fired, the
answer is in the record.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dlai.entity import Track
from dlai.interaction import Interaction

MODE = "RECOMMEND_ONLY"


@dataclass(frozen=True)
class Recommendation:
    t_ms: int
    rule: str
    subject_mac: str
    action: str          # always a recommendation, never an enforcement
    severity: str        # "info" | "warn" | "high"
    evidence: dict = field(default_factory=dict)
    mode: str = MODE


class PolicyEngine:
    def __init__(self, flicker_warn_transitions: int = 6):
        self.flicker_warn = flicker_warn_transitions
        self.recommendations: list[Recommendation] = []

    def evaluate_track(self, track: Track,
                       interactions: list[Interaction]) -> list[Recommendation]:
        out: list[Recommendation] = []
        exit_events = [i for i in interactions if i.kind == "exit_zone"]
        checkout_events = [i for i in interactions if i.kind == "at_checkout"]

        if exit_events and not checkout_events:
            ev = exit_events[0]
            out.append(Recommendation(
                t_ms=ev.t_end_ms, rule="exit_without_checkout_presence",
                subject_mac=track.mac,
                action="recommend attendant check at exit gate",
                severity="warn",
                evidence={"exit_at": (ev.x, ev.y),
                          "checkout_interactions": 0,
                          "note": "position is inferred; absence of checkout "
                                  "presence may be a sensing gap, not theft"}))

        if track.flicker_transitions >= self.flicker_warn:
            last = track.last_fix()
            out.append(Recommendation(
                t_ms=last.t_ms if last else 0,
                rule="position_flicker",
                subject_mac=track.mac,
                action="recommend treating this track's positions as "
                       "low-trust; do not act on single fixes",
                severity="info",
                evidence={"flicker_transitions": track.flicker_transitions,
                          "note": "oscillation around the min-AP rule — the "
                                  "metric that matters"}))
        self.recommendations.extend(out)
        return out
