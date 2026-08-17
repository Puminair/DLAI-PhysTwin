"""Layer 2 — video sensing: what a CCTV camera can actually see.

May import: stdlib, config, world.geometry (to observe occlusion).

A camera's placement and aim are Layer-1 facts; what it *sees* is a
Layer-2 question, exactly as an RF sensor's position is a fact but its
coverage is not. The model mirrors propagation.py, but light is binary:
unlike RF, it does not attenuate through a shelf — any solid fixture on
the line of sight blocks the view outright. So a cell is seen iff it is
(1) within the horizontal field-of-view wedge, (2) within useful range,
and (3) on an unobstructed 3D line from the camera to the target height.

This reproduces the same lesson the RF model teaches, inverted: a camera
at 3.2 m looking *down* clears a 2.0 m gondola only where the depression
geometry allows — the height-aware occlusion test decides it, cell by
cell, the same way segment_crossings decides the RF path.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Camera:
    camera_id: str
    x: float
    y: float
    z: float
    model: str
    yaw_deg: float
    fov_deg: float
    range_m: float
    zone: str


def load_cameras(path: str | Path) -> list[Camera]:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return [Camera(camera_id=c["camera_id"], x=c["x"], y=c["y"], z=c["z"],
                   model=c["model"], yaw_deg=c["yaw_deg"], fov_deg=c["fov_deg"],
                   range_m=c["range_m"], zone=c["zone"])
            for c in doc["cameras"]]


def _angle_within(cam: Camera, x: float, y: float) -> bool:
    """Is the target inside the camera's horizontal FOV wedge?"""
    bearing = math.degrees(math.atan2(y - cam.y, x - cam.x))
    diff = (bearing - cam.yaw_deg + 180.0) % 360.0 - 180.0
    return abs(diff) <= cam.fov_deg / 2.0


class VisionField:
    """All cameras plus the geometry needed to clip their views."""

    def __init__(self, cameras: list[Camera], geometry, cfg):
        self.cameras = cameras
        self.geometry = geometry
        self.cfg = cfg
        self.target_z = cfg.get("vision.target_z_m")
        self._occluders = set(cfg.get("vision.occluders"))

    def sees(self, cam: Camera, x: float, y: float) -> bool:
        """Does this camera have a clear view of (x, y) at target height?"""
        d = math.hypot(x - cam.x, y - cam.y)
        if d > cam.range_m or d < 0.3:
            return d < 0.3    # directly underneath counts as seen
        if not _angle_within(cam, x, y):
            return False
        crossings = self.geometry.segment_crossings(
            (cam.x, cam.y, cam.z), (x, y, self.target_z))
        return not any(m in self._occluders for m in crossings)

    def cameras_seeing(self, x: float, y: float) -> list[str]:
        return [c.camera_id for c in self.cameras if self.sees(c, x, y)]

    def footprint(self, cam: Camera, cell_m: float = 1.0) -> list[tuple[float, float]]:
        """Sampled ground cells this camera actually sees (for drawing)."""
        out = []
        r = cam.range_m
        x0, x1 = cam.x - r, cam.x + r
        y0, y1 = cam.y - r, cam.y + r
        gx = x0
        while gx <= x1:
            gy = y0
            while gy <= y1:
                if self.sees(cam, gx, gy):
                    out.append((round(gx, 2), round(gy, 2)))
                gy += cell_m
            gx += cell_m
        return out
