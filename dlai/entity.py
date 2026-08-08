"""Layer 3 — entity resolution: observations -> tracked entities.

May import: stdlib, dlai.*. NEVER world/.

Position is a distribution, not a point. With two sensors there are two
intersection candidates and no way to choose; resolution is by
ELIMINATION using the floor plan (impossible inside a fixture, outside
the envelope, or unreachable at walking speed), never by wishing the
ambiguity away. Variance is carried through to the output and never
collapsed early.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from dlai.floorplan import FloorPlan
from dlai.ingest import NormalisedObservation, SensorSite

# Log-distance path-loss inversion for ranging. Same physics family as
# Layer 2 but deliberately simpler — Layer 3 only knows textbook RF, not
# the building's private attenuation truth.
_FSPL_C = 27.55
_F_MHZ = 2437.0


def rssi_to_distance_m(rssi_dbm: float, eirp_guess_dbm: float = 17.0,
                       gain_guess_dbi: float = 4.0) -> float:
    """Invert FSPL for a rough range estimate. Optimistic under occlusion."""
    path_loss = eirp_guess_dbm + gain_guess_dbi - rssi_dbm
    exp = (path_loss - 20.0 * math.log10(_F_MHZ) + _FSPL_C) / 20.0
    return max(0.5, 10.0 ** exp)


@dataclass
class PositionEstimate:
    t_ms: int
    x: float
    y: float
    variance_m2: float
    n_aps: int
    candidates: list[tuple[float, float]]     # survivors of elimination
    confidence: str                           # always "inferred"
    gaps: list[str] = field(default_factory=list)


@dataclass
class Track:
    mac: str
    estimates: list[PositionEstimate] = field(default_factory=list)
    flicker_transitions: int = 0
    _last_ok: bool | None = None

    def last_fix(self) -> PositionEstimate | None:
        return self.estimates[-1] if self.estimates else None


class EntityResolver:
    def __init__(self, sites: dict[str, SensorSite], plan: FloorPlan,
                 min_aps: int = 3):
        self.sites = sites
        self.plan = plan
        self.min_aps = min_aps
        self.tracks: dict[str, Track] = {}

    def consume(self, obs: NormalisedObservation) -> PositionEstimate | None:
        track = self.tracks.setdefault(obs.mac, Track(mac=obs.mac))
        heard = [(self.sites[mac], rssi) for mac, rssi in obs.rssi_dbm
                 if mac in self.sites]
        ok = len(heard) >= self.min_aps

        # flicker bookkeeping: oscillation around the min-AP rule
        if track._last_ok is not None and ok != track._last_ok:
            track.flicker_transitions += 1
        track._last_ok = ok
        if not ok:
            return None

        heard.sort(key=lambda h: h[1], reverse=True)
        est = self._resolve(obs, heard[:6], track)
        track.estimates.append(est)
        return est

    def _resolve(self, obs: NormalisedObservation,
                 heard: list[tuple[SensorSite, float]],
                 track: Track) -> PositionEstimate:
        prev_est = track.last_fix()
        prev = (prev_est.x, prev_est.y) if prev_est else None
        dt_s = ((obs.seen_time_ms - prev_est.t_ms) / 1000.0) if prev_est else 0.0

        # candidate generation: pairwise circle intersections of the
        # strongest ranged sensors
        candidates: list[tuple[float, float]] = []
        for i in range(len(heard)):
            for j in range(i + 1, len(heard)):
                s1, r1 = heard[i]
                s2, r2 = heard[j]
                candidates.extend(_circle_intersections(
                    (s1.x, s1.y), rssi_to_distance_m(r1),
                    (s2.x, s2.y), rssi_to_distance_m(r2)))
        # elimination — geometry rules out where the cart cannot be
        survivors = [c for c in candidates
                     if self.plan.possible(c[0], c[1], prev, dt_s)]
        gaps = list(obs.gaps)

        if survivors:
            # score survivors by agreement with all ranges
            def err(c):
                return sum((math.dist(c, (s.x, s.y)) - rssi_to_distance_m(r)) ** 2
                           for s, r in heard)
            survivors.sort(key=err)
            keep = survivors[:8]
            wx = sum(c[0] for c in keep) / len(keep)
            wy = sum(c[1] for c in keep) / len(keep)
            var = (sum((c[0] - wx) ** 2 + (c[1] - wy) ** 2 for c in keep)
                   / len(keep)) or 0.25
        elif obs.cloud_location is not None:
            wx, wy = obs.cloud_location["x"], obs.cloud_location["y"]
            var = max(obs.cloud_location["variance"], 4.0)
            keep = [(wx, wy)]
            gaps.append("elimination_left_no_candidates_fell_back_to_cloud")
        else:
            # RSSI-weighted centroid of heard sensors — worst fallback
            w = [10.0 ** (r / 20.0) for _, r in heard]
            tot = sum(w)
            wx = sum(s.x * wi for (s, _), wi in zip(heard, w)) / tot
            wy = sum(s.y * wi for (s, _), wi in zip(heard, w)) / tot
            var = 25.0
            keep = [(wx, wy)]
            gaps.append("centroid_fallback")

        return PositionEstimate(
            t_ms=obs.seen_time_ms, x=round(wx, 2), y=round(wy, 2),
            variance_m2=round(var, 2), n_aps=len(obs.rssi_dbm),
            candidates=keep, confidence="inferred", gaps=gaps)


def _circle_intersections(c1, r1, c2, r2) -> list[tuple[float, float]]:
    d = math.dist(c1, c2)
    if d < 1e-6:
        return []
    if d > r1 + r2:
        # circles don't touch: nearest-approach midpoint as weak candidate
        t = r1 / (r1 + r2)
        return [(c1[0] + t * (c2[0] - c1[0]), c1[1] + t * (c2[1] - c1[1]))]
    if d < abs(r1 - r2):
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h2 = r1 * r1 - a * a
    h = math.sqrt(max(0.0, h2))
    mx = c1[0] + a * (c2[0] - c1[0]) / d
    my = c1[1] + a * (c2[1] - c1[1]) / d
    if h < 1e-6:
        return [(mx, my)]
    ox = h * (c2[1] - c1[1]) / d
    oy = -h * (c2[0] - c1[0]) / d
    return [(mx + ox, my + oy), (mx - ox, my - oy)]
