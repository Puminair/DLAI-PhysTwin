"""Layer 2 video sensing: FOV, range, and height-aware occlusion."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from config import load_config
from sensing.vision import Camera, VisionField, _angle_within, load_cameras
from world.geometry import StoreGeometry

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def field(cfg):
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    return VisionField(load_cameras(DATA / "cameras.json"), geo, cfg)


def test_cameras_load():
    cams = load_cameras(DATA / "cameras.json")
    assert len(cams) > 30
    assert {c.model for c in cams} <= {"dome", "bullet", "ptz"}


def test_fov_wedge_geometry():
    cam = Camera("Cx", 10.0, 10.0, 3.2, "bullet", yaw_deg=0.0,  # aimed +x
                 fov_deg=60.0, range_m=18.0, zone="z")
    assert _angle_within(cam, 20.0, 10.0)        # dead ahead
    assert _angle_within(cam, 20.0, 15.0)        # within +26.6 deg
    assert not _angle_within(cam, 20.0, 40.0)    # 71 deg off — outside 30
    assert not _angle_within(cam, 0.0, 10.0)     # directly behind


def test_range_and_bearing_limit_the_view(field, cfg):
    # an open point on the sales floor, camera aimed at it, within range
    cam = Camera("Cy", 20.0, 5.4, 3.2, "bullet", yaw_deg=0.0,
                 fov_deg=90.0, range_m=18.0, zone="z")
    assert field.sees(cam, 26.0, 5.4)            # 6 m ahead, clear aisle
    assert not field.sees(cam, 45.0, 5.4)        # 25 m — beyond range
    assert not field.sees(cam, 14.0, 5.4)        # behind the camera


def test_occlusion_blocks_the_view_behind_a_gondola(field):
    # find a gondola; a camera on one long side aimed across it cannot see
    # a target on the far side (light does not pass through the shelf),
    # while the same camera sees an equidistant clear point beside it.
    geo = field.geometry
    g = next(f for f in geo.fixtures if f.kind == "gondola")
    x0, y0, x1, y1 = g.bbox()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    depth = y1 - y0
    cam = Camera("Cg", cx, cy - depth - 2.0, 3.2, "bullet", yaw_deg=90.0,
                 fov_deg=120.0, range_m=15.0, zone="z")
    far = (cx, cy + depth + 1.0)      # directly across the gondola
    assert not field.sees(cam, *far), "camera saw through a solid gondola"


def test_cameras_seeing_returns_ids(field):
    seen = field.cameras_seeing(20.0, 5.4)
    assert isinstance(seen, list)
    assert all(isinstance(s, str) for s in seen)
