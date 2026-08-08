"""Layer 3 — interaction classification over resolved tracks.

May import: stdlib, dlai.*. NEVER world/.

Classifies what a tracked entity appears to be doing — dwell at a
fixture, transit, checkout presence, exit — from position estimates
alone. Every interaction is an inference and says so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from dlai.entity import Track

CHECKOUT_X_MAX = 6.5      # deployment knowledge: checkout band, west wall
EXIT_Y_MAX = 3.0          # deployment knowledge: gate band, south wall


@dataclass(frozen=True)
class Interaction:
    mac: str
    t_start_ms: int
    t_end_ms: int
    kind: str          # "dwell" | "transit" | "at_checkout" | "exit_zone"
    x: float
    y: float
    confidence: str    # always "inferred"


def classify_track(track: Track, dwell_speed_ms: float = 0.15,
                   min_dwell_s: float = 20.0) -> list[Interaction]:
    out: list[Interaction] = []
    est = track.estimates
    if len(est) < 2:
        return out
    dwell_start = None
    for prev, cur in zip(est, est[1:]):
        dt = (cur.t_ms - prev.t_ms) / 1000.0
        if dt <= 0:
            continue
        speed = math.dist((prev.x, prev.y), (cur.x, cur.y)) / dt
        if cur.x <= CHECKOUT_X_MAX and cur.y > EXIT_Y_MAX:
            out.append(Interaction(track.mac, prev.t_ms, cur.t_ms,
                                   "at_checkout", cur.x, cur.y, "inferred"))
            dwell_start = None
        elif cur.y <= EXIT_Y_MAX:
            out.append(Interaction(track.mac, prev.t_ms, cur.t_ms,
                                   "exit_zone", cur.x, cur.y, "inferred"))
            dwell_start = None
        elif speed < dwell_speed_ms:
            if dwell_start is None:
                dwell_start = prev
        else:
            if dwell_start is not None:
                held_s = (prev.t_ms - dwell_start.t_ms) / 1000.0
                if held_s >= min_dwell_s:
                    out.append(Interaction(track.mac, dwell_start.t_ms,
                                           prev.t_ms, "dwell",
                                           dwell_start.x, dwell_start.y,
                                           "inferred"))
                dwell_start = None
            out.append(Interaction(track.mac, prev.t_ms, cur.t_ms,
                                   "transit", cur.x, cur.y, "inferred"))
    if dwell_start is not None:
        held_s = (est[-1].t_ms - dwell_start.t_ms) / 1000.0
        if held_s >= min_dwell_s:
            out.append(Interaction(track.mac, dwell_start.t_ms, est[-1].t_ms,
                                   "dwell", dwell_start.x, dwell_start.y,
                                   "inferred"))
    return out
