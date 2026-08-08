"""Geometry: crossing counts, z-awareness, fast-path equivalence."""
from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from world.geometry import StoreGeometry, segment_in_convex

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def geo():
    return StoreGeometry(DATA / "store_layer1.geojson")


def test_segment_over_gondola_top_does_not_count(geo):
    # find a gondola and shoot a ray fully above it (z 3.0 -> 2.5)
    g = next(f for f in geo.fixtures if f.kind == "gondola")
    x0, y0, x1, y1 = g.bbox()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    high = geo.segment_crossings((cx - 3, cy, 3.0), (cx + 3, cy, 2.5))
    assert high.get("gondola", 0) == 0, "ray above gondola top must not cross"
    low = geo.segment_crossings((cx - 3, cy, 1.0), (cx + 3, cy, 1.0))
    assert low.get("gondola", 0) >= 1, "ray at panel height must cross"


def test_sensor_looks_over_nearby_gondola_but_not_far_one(geo):
    # sensor 3.0 m, panel 1.0 m: z(t) = 3 - 2t crosses 2.0 at t = 0.5,
    # so a gondola in the NEAR half of the path is overlooked and the
    # same gondola in the FAR half blocks. This is the whole problem.
    # Rays run perpendicular to the row, short enough to miss other rows.
    g = next(f for f in geo.fixtures if f.fid == "gondola_r00_b00")
    x0, y0, x1, y1 = g.bbox()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    near = geo.segment_crossings((cx, cy - 1, 3.0), (cx, cy + 3, 1.0))
    far = geo.segment_crossings((cx, cy - 3, 3.0), (cx, cy + 1, 1.0))
    assert near.get("gondola", 0) == 0
    assert far.get("gondola", 0) >= 1


def test_fast_path_matches_pure_python(geo):
    rng = random.Random(42)
    for _ in range(200):
        a = (rng.uniform(0, 100), rng.uniform(0, 55), rng.uniform(0.5, 3.5))
        b = (rng.uniform(0, 100), rng.uniform(0, 55), rng.uniform(0.5, 3.5))
        fast = geo._crossings_fast(a, b)
        slow = Counter()
        for fx in geo.fixtures:
            span = segment_in_convex((a[0], a[1]), (b[0], b[1]), fx.ring)
            if span:
                t0, t1 = span
                zs = sorted([a[2] + t0 * (b[2] - a[2]), a[2] + t1 * (b[2] - a[2])])
                if zs[1] >= fx.z0 and zs[0] <= fx.z1:
                    slow[fx.material] += 1
        assert fast == slow, f"fast path diverges for {a}->{b}"


def test_envelope_containment(geo):
    assert geo.inside_envelope(25, 25)
    assert not geo.inside_envelope(-1, 25)
    assert not geo.inside_envelope(80, 5)   # inside bbox, outside cut corner


def test_column_count_is_exactly_77(geo):
    assert sum(1 for f in geo.fixtures if f.kind == "column") == 77
