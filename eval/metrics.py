"""Evaluation metrics: precision, recall, position error, flicker.

May import: stdlib. Pure functions over (truth, estimate) pairs.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass
class PositionErrorStats:
    n: int
    mean_m: float
    median_m: float
    p90_m: float

    @classmethod
    def from_errors(cls, errors: list[float]) -> "PositionErrorStats | None":
        if not errors:
            return None
        s = sorted(errors)
        return cls(n=len(s), mean_m=round(statistics.mean(s), 2),
                   median_m=round(s[len(s) // 2], 2),
                   p90_m=round(s[int(len(s) * 0.9)], 2))


def position_errors(truth_xy_by_t: dict[int, tuple[float, float]],
                    estimates: list) -> list[float]:
    """Match estimates to truth at the nearest truth timestamp (<= 5 s off)."""
    if not truth_xy_by_t:
        return []
    times = sorted(truth_xy_by_t)
    out = []
    for est in estimates:
        t = min(times, key=lambda tt: abs(tt - est.t_ms))
        if abs(t - est.t_ms) > 5000:
            continue
        tx, ty = truth_xy_by_t[t]
        out.append(math.dist((tx, ty), (est.x, est.y)))
    return out


def detection_prf(n_true_present: int, n_detected: int,
                  n_false_tracks: int) -> dict:
    """Detection precision/recall over device-presence.

    A truth device counts detected if Layer 3 produced any track for its
    MAC; a false track is a resolved track with no corresponding truth
    device (should be ~0 here — the pipeline invents nothing).
    """
    tp = n_detected
    precision = tp / (tp + n_false_tracks) if (tp + n_false_tracks) else 0.0
    recall = tp / n_true_present if n_true_present else 0.0
    return {"true_present": n_true_present, "detected": tp,
            "false_tracks": n_false_tracks,
            "precision": round(precision, 3), "recall": round(recall, 3)}


def flicker_rate(transitions: int, n_estimates: int) -> float:
    """Transitions across the min-AP rule per estimate — the metric that
    matters. A cart oscillating around 3 APs zigzags while standing still."""
    return round(transitions / n_estimates, 3) if n_estimates else 0.0
