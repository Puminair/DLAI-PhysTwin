"""Layer 3 — independent floor-plan knowledge for elimination tests.

May import: stdlib only. Deliberately NOT world.geometry: the import ban
is what keeps the blind test honest. The floor plan is deployable
knowledge (a drawing that ships with the site), so reading the GeoJSON
*artifact* is allowed; reading the live world code is not. The small
duplication with world/geometry.py is the price of that separation.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlanFixture:
    fid: str
    kind: str
    ring: tuple[tuple[float, float], ...]
    z0: float
    z1: float


def _point_in_ring(x: float, y: float, ring) -> bool:
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


class FloorPlan:
    def __init__(self, geojson_path: str | Path):
        with open(geojson_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.envelope: tuple = ()
        self.fixtures: list[PlanFixture] = []
        for f in doc["features"]:
            ring = tuple((float(x), float(y)) for x, y in f["geometry"]["coordinates"][0])
            if f["id"] == "envelope":
                self.envelope = ring
                continue
            p = f["properties"]
            self.fixtures.append(PlanFixture(fid=f["id"], kind=p["kind"], ring=ring,
                                             z0=float(p["z0_m"]), z1=float(p["z1_m"])))
        self._solid = [fx for fx in self.fixtures
                       if fx.kind in ("gondola", "column", "cold_room", "racking",
                                      "chiller", "room", "wall", "endcap",
                                      "promo_pallet", "produce_table")]

    def inside_envelope(self, x: float, y: float) -> bool:
        return _point_in_ring(x, y, self.envelope)

    def inside_solid_fixture(self, x: float, y: float, z: float = 1.0) -> bool:
        """True if (x, y) at height z is inside a fixture a cart cannot be in."""
        for fx in self._solid:
            if fx.z0 <= z <= fx.z1:
                x0 = min(p[0] for p in fx.ring)
                x1 = max(p[0] for p in fx.ring)
                if x0 - 0.01 <= x <= x1 + 0.01 and _point_in_ring(x, y, fx.ring):
                    return True
        return False

    def possible(self, x: float, y: float, prev: tuple[float, float] | None,
                 dt_s: float, max_speed_ms: float = 1.5) -> bool:
        """Elimination — the whole value of geometry to Layer 3.

        Not "where is the cart" but "where can it NOT be": outside the
        envelope, inside a solid fixture, or unreachable from the last
        fix at walking speed.
        """
        if not self.inside_envelope(x, y):
            return False
        if self.inside_solid_fixture(x, y):
            return False
        if prev is not None and dt_s > 0:
            if math.dist((x, y), prev) > max_speed_ms * dt_s + 1.0:
                return False
        return True
