"""RF ↔ video coverage fusion — where each modality covers the other's gaps.

May import: stdlib, config, world, sensing. (Not dlai — fusion is a
deployment/physics question about coverage, not an inference over
observations.)

For each floor cell at cart-panel height it asks two independent
questions — can RF position here (>= 3 sensors over the threshold)? can a
camera see here (>= 1 clear view)? — and crosses them:

  both        RF positions AND video sees      (belt and braces)
  rf_only     RF positions, video blind        (locate but cannot watch)
  video_only  video sees, RF blind             (watch but cannot locate)
  neither     blind in BOTH                     (the dangerous cell)

The `neither` set is the whole point: it is the physical realisation of
the `rf_video_desync` attack in the catalogue — the place an adversary is
neither located nor seen. Reallocating sensors or adding a camera is
judged by how much it shrinks that set.
"""
from __future__ import annotations

import json
from pathlib import Path

from sensing.sensor import SensorField
from sensing.vision import VisionField


def fuse(sensor_field: SensorField, vision_field: VisionField, cfg,
         band: str = "2.4", cell_m: float = 2.0) -> dict:
    geo = sensor_field.geometry
    x_split = cfg.get("areas.x_split_m")
    panel_z = cfg.get("heights.cart_panel_z_m")
    eirp = cfg.get("cart_uplink.eirp_dbm")[band]
    min_aps = cfg.get("rules.min_aps_for_position")
    solid = ("gondola", "column", "cold_room", "racking", "wall", "room")

    cells = []
    tally = {z: {"both": 0, "rf_only": 0, "video_only": 0, "neither": 0}
             for z in ("sales_floor", "back_of_house")}

    y = cell_m / 2
    while y < 55.0:
        x = cell_m / 2
        while x < 100.0:
            if geo.inside_envelope(x, y) and not any(
                    f.material in solid for f in geo.fixtures_at(x, y, panel_z)):
                rf = len(sensor_field.hear_device(
                    band=band, eirp_dbm=eirp, device_xyz=(x, y, panel_z),
                    shopper_xy=[])) >= min_aps
                video = bool(vision_field.cameras_seeing(x, y))
                cls = ("both" if rf and video else "rf_only" if rf
                       else "video_only" if video else "neither")
                zone = "sales_floor" if x < x_split else "back_of_house"
                tally[zone][cls] += 1
                cells.append({"x": round(x, 1), "y": round(y, 1), "class": cls})
            x += cell_m
        y += cell_m

    def pct(part, whole):
        return round(100.0 * part / whole, 1) if whole else 0.0

    summary = {}
    for zone, t in tally.items():
        tot = sum(t.values())
        summary[zone] = {"cells": tot,
                         **{k: pct(v, tot) for k, v in t.items()},
                         "counts": t}
    return {"band": band, "cell_m": cell_m, "panel_z_m": panel_z,
            "cells": cells, "summary": summary}


def write_fusion_report(sensor_field, vision_field, cfg, out_path: str | Path,
                        band: str = "2.4", cell_m: float = 2.0) -> dict:
    report = fuse(sensor_field, vision_field, cfg, band, cell_m)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    return report
