"""Layer 1 — geometry: GeoJSON load, spatial index, occlusion tests.

May import: stdlib, config. No sensor concepts here — this module
answers purely geometric questions ("what does this segment cross"),
and downstream layers attach meaning to the answers.

The spatial index is a uniform grid (pure Python, no rtree dependency):
fixtures register in every 2 m cell their bbox overlaps, and a segment
query walks the cells it passes through. All fixture rings are convex
and CCW, so segment/polygon intersection is Cyrus–Beck clipping.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]

CELL_M = 2.0


@dataclass(frozen=True)
class Fixture:
    fid: str
    kind: str
    material: str
    ring: tuple[Point2, ...]  # closed, CCW
    z0: float
    z1: float
    zone: str

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.ring]
        ys = [p[1] for p in self.ring]
        return min(xs), min(ys), max(xs), max(ys)


def point_in_convex(x: float, y: float, ring: tuple[Point2, ...]) -> bool:
    """CCW convex ring containment (boundary counts as inside)."""
    for i in range(len(ring) - 1):
        ex, ey = ring[i + 1][0] - ring[i][0], ring[i + 1][1] - ring[i][1]
        px, py = x - ring[i][0], y - ring[i][1]
        if ex * py - ey * px < -1e-9:
            return False
    return True


def segment_in_convex(a: Point2, b: Point2, ring: tuple[Point2, ...]) -> tuple[float, float] | None:
    """Cyrus–Beck: the t-interval of segment a->b inside a CCW convex ring.

    Returns (t_enter, t_exit) in [0, 1], or None if the segment misses.
    """
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for i in range(len(ring) - 1):
        # inward normal of CCW edge (p -> q) is (-ey, ex)
        px, py = ring[i]
        qx, qy = ring[i + 1]
        nx, ny = -(qy - py), (qx - px)
        num = nx * (a[0] - px) + ny * (a[1] - py)   # >0 means a is inside this edge
        den = nx * dx + ny * dy
        if abs(den) < 1e-12:
            if num < 0:
                return None
            continue
        t = -num / den
        if den > 0:      # entering
            t0 = max(t0, t)
        else:            # leaving
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (t0, t1)


def point_segment_dist(p: Point2, a: Point2, b: Point2) -> tuple[float, float]:
    """Distance from p to segment a->b, and the parameter t of the foot."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.dist(p, a), 0.0
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy)), t


class StoreGeometry:
    """Loaded floor plan with a uniform-grid spatial index."""

    def __init__(self, geojson_path: str | Path):
        with open(geojson_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.provenance = doc.get("provenance", {})
        self.envelope: tuple[Point2, ...] = ()
        self.fixtures: list[Fixture] = []
        for f in doc["features"]:
            p = f["properties"]
            ring = tuple((float(x), float(y)) for x, y in f["geometry"]["coordinates"][0])
            if f["id"] == "envelope":
                self.envelope = ring
                continue
            self.fixtures.append(Fixture(
                fid=f["id"], kind=p["kind"], material=p["material"],
                ring=ring, z0=float(p["z0_m"]), z1=float(p["z1_m"]),
                zone=p["zone"]))
        if not self.envelope:
            raise ValueError("geojson has no envelope feature")

        self._grid: dict[tuple[int, int], list[int]] = {}
        for idx, fx in enumerate(self.fixtures):
            x0, y0, x1, y1 = fx.bbox()
            for cx in range(int(x0 // CELL_M), int(x1 // CELL_M) + 1):
                for cy in range(int(y0 // CELL_M), int(y1 // CELL_M) + 1):
                    self._grid.setdefault((cx, cy), []).append(idx)
        self._build_fast_path()

    def _build_fast_path(self) -> None:
        """Vectorised AABB arrays for axis-aligned rect fixtures.

        segment_crossings is the hot path (every sensor x device pair,
        every tick). When numpy is available, all rectangular fixtures
        are tested at once with the slab method; non-rect fixtures (none
        in the parametric dataset, but the format allows them) fall back
        to per-fixture Cyrus-Beck.
        """
        self._np = None
        self._slow_idx: list[int] = list(range(len(self.fixtures)))
        try:
            import numpy as np
        except ImportError:
            return

        def is_rect(fx: Fixture) -> bool:
            if len(fx.ring) != 5:
                return False
            x0, y0, x1, y1 = fx.bbox()
            corners = {(x0, y0), (x1, y0), (x1, y1), (x0, y1)}
            return all((round(p[0], 6), round(p[1], 6)) in
                       {(round(cx, 6), round(cy, 6)) for cx, cy in corners}
                       for p in fx.ring[:-1])

        rect_idx = [i for i, fx in enumerate(self.fixtures) if is_rect(fx)]
        self._slow_idx = [i for i in range(len(self.fixtures)) if i not in set(rect_idx)]
        if not rect_idx:
            return
        boxes = np.array([self.fixtures[i].bbox() for i in rect_idx])
        self._np = np
        self._bx0, self._by0, self._bx1, self._by1 = boxes.T
        self._bz0 = np.array([self.fixtures[i].z0 for i in rect_idx])
        self._bz1 = np.array([self.fixtures[i].z1 for i in rect_idx])
        mats = sorted({self.fixtures[i].material for i in rect_idx})
        self._mat_names = mats
        self._mat_ids = np.array([mats.index(self.fixtures[i].material)
                                  for i in rect_idx])

    def _crossings_fast(self, a: Point3, b: Point3,
                        exclude_mask=None) -> Counter | None:
        if self._np is None:
            return None
        np = self._np
        ax, ay, az = a
        dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
        with np.errstate(divide="ignore", invalid="ignore"):
            tx0 = (self._bx0 - ax) / dx if dx != 0 else None
            if dx != 0:
                tx1 = (self._bx1 - ax) / dx
                txmin, txmax = np.minimum(tx0, tx1), np.maximum(tx0, tx1)
            else:
                inside = (self._bx0 <= ax) & (ax <= self._bx1)
                txmin = np.where(inside, -np.inf, np.inf)
                txmax = np.where(inside, np.inf, -np.inf)
            if dy != 0:
                ty0 = (self._by0 - ay) / dy
                ty1 = (self._by1 - ay) / dy
                tymin, tymax = np.minimum(ty0, ty1), np.maximum(ty0, ty1)
            else:
                inside = (self._by0 <= ay) & (ay <= self._by1)
                tymin = np.where(inside, -np.inf, np.inf)
                tymax = np.where(inside, np.inf, -np.inf)
        t0 = np.maximum(np.maximum(txmin, tymin), 0.0)
        t1 = np.minimum(np.minimum(txmax, tymax), 1.0)
        hit = t0 <= t1
        if hit.any():
            # inf*0 -> nan on non-hit rows is harmless: nan comparisons
            # are False and those rows are already excluded by `hit`
            with np.errstate(invalid="ignore"):
                z_a = az + t0 * dz
                z_b = az + t1 * dz
                z_lo = np.minimum(z_a, z_b)
                z_hi = np.maximum(z_a, z_b)
                hit &= (z_hi >= self._bz0) & (z_lo <= self._bz1)
        if exclude_mask is not None:
            hit &= ~exclude_mask
        counts: Counter = Counter()
        if hit.any():
            binc = np.bincount(self._mat_ids[hit], minlength=len(self._mat_names))
            for mid, n in enumerate(binc):
                if n:
                    counts[self._mat_names[mid]] = int(n)
        return counts

    # -- point queries -------------------------------------------------
    def inside_envelope(self, x: float, y: float) -> bool:
        return point_in_convex(x, y, self.envelope)

    def fixtures_at(self, x: float, y: float, z: float | None = None) -> list[Fixture]:
        out = []
        for idx in self._grid.get((int(x // CELL_M), int(y // CELL_M)), []):
            fx = self.fixtures[idx]
            if z is not None and not (fx.z0 <= z <= fx.z1):
                continue
            if point_in_convex(x, y, fx.ring):
                out.append(fx)
        return out

    def zone_of(self, x: float, y: float, x_split: float) -> str:
        if not self.inside_envelope(x, y):
            return "outside"
        return "sales_floor" if x < x_split else "back_of_house"

    # -- segment queries ----------------------------------------------
    def _candidates(self, a: Point2, b: Point2) -> set[int]:
        """Supercover grid walk between a and b (padded by one cell)."""
        out: set[int] = set()
        steps = max(1, int(math.dist(a, b) / CELL_M) * 2)
        for i in range(steps + 1):
            t = i / steps
            cx = int((a[0] + t * (b[0] - a[0])) // CELL_M)
            cy = int((a[1] + t * (b[1] - a[1])) // CELL_M)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    out.update(self._grid.get((cx + dx, cy + dy), ()))
        return out

    def segment_crossings(self, a: Point3, b: Point3,
                          exclude: set[str] | None = None) -> Counter:
        """Count fixtures the 3D segment a->b passes through, by material.

        A fixture counts once if the segment's xy-projection intersects
        its ring AND the segment's z over that intersection overlaps the
        fixture's [z0, z1]. This is the occlusion primitive everything
        downstream calls.
        """
        a2, b2 = (a[0], a[1]), (b[0], b[1])
        if self._np is not None and not exclude and not self._slow_idx:
            fast = self._crossings_fast(a, b)
            if fast is not None:
                return fast
        counts: Counter = Counter()
        for idx in self._candidates(a2, b2):
            fx = self.fixtures[idx]
            if exclude and fx.fid in exclude:
                continue
            span = segment_in_convex(a2, b2, fx.ring)
            if span is None:
                continue
            t0, t1 = span
            z_lo = min(a[2] + t0 * (b[2] - a[2]), a[2] + t1 * (b[2] - a[2]))
            z_hi = max(a[2] + t0 * (b[2] - a[2]), a[2] + t1 * (b[2] - a[2]))
            if z_hi >= fx.z0 and z_lo <= fx.z1:
                counts[fx.material] += 1
        return counts

    def reflector_surfaces(self) -> list[tuple[Point2, Point2, float]]:
        """Long vertical faces usable as first-order reflectors.

        Returns (edge_a, edge_b, top_z) for the two long faces of every
        steel-backed fixture (gondolas and racking). Steel gondola backs
        are excellent reflectors and poor transmitters.
        """
        out = []
        for fx in self.fixtures:
            if fx.material not in ("gondola", "racking"):
                continue
            ring = fx.ring
            edges = [(ring[i], ring[i + 1]) for i in range(len(ring) - 1)]
            edges.sort(key=lambda e: -math.dist(e[0], e[1]))
            for e in edges[:2]:
                out.append((e[0], e[1], fx.z1))
        return out
