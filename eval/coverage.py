"""Coverage analysis at cart-panel height — the plane that decides everything.

May import: stdlib, config, world, sensing. (Not dlai — coverage is a
physics question, not an inference one.)

For each 2 m grid cell at z = 1.00 m, count sensors whose modelled
uplink RSSI clears the location threshold. A cell with >= 3 is
positionable; below 3 it is blind. Reflections are evaluated only where
the direct model fails the 3-AP rule (5/6 GHz only — measured effect at
2.4 is zero).
"""
from __future__ import annotations

import json
from pathlib import Path

from sensing.sensor import SensorField


def coverage_grid(field: SensorField, cfg, band: str,
                  cell_m: float = 2.0) -> dict:
    geo = field.geometry
    x_split = cfg.get("areas.x_split_m")
    panel_z = cfg.get("heights.cart_panel_z_m")
    eirp = cfg.get("cart_uplink.eirp_dbm")[band]
    min_aps = cfg.get("rules.min_aps_for_position")

    cells = []
    zone_tot = {"sales_floor": 0, "back_of_house": 0}
    zone_ok = {"sales_floor": 0, "back_of_house": 0}
    zone_blind = {"sales_floor": 0, "back_of_house": 0}

    y = cell_m / 2
    while y < 55.0:
        x = cell_m / 2
        while x < 100.0:
            if geo.inside_envelope(x, y):
                solid = geo.fixtures_at(x, y, panel_z)
                if not any(f.material in ("gondola", "column", "cold_room",
                                          "racking", "wall", "room")
                           for f in solid):
                    recs = field.hear_device(
                        band=band, eirp_dbm=eirp,
                        device_xyz=(x, y, panel_z), shopper_xy=[])
                    n = len(recs)
                    zone = "sales_floor" if x < x_split else "back_of_house"
                    zone_tot[zone] += 1
                    if n >= min_aps:
                        zone_ok[zone] += 1
                    if n == 0:
                        zone_blind[zone] += 1
                    cells.append({"x": round(x, 1), "y": round(y, 1),
                                  "n_aps": n,
                                  "best_rssi": recs[0]["rssi"] if recs else None})
            x += cell_m
        y += cell_m

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    return {
        "band": band,
        "panel_z_m": panel_z,
        "cell_m": cell_m,
        "cells": cells,
        "summary": {
            zone: {"cells": zone_tot[zone],
                   "ge3ap_pct": pct(zone_ok[zone], zone_tot[zone]),
                   "blind_pct": pct(zone_blind[zone], zone_tot[zone])}
            for zone in zone_tot
        },
    }


def write_coverage_report(field: SensorField, cfg, out_path: str | Path,
                          bands: tuple[str, ...] = ("2.4", "5", "6"),
                          cell_m: float = 2.0) -> dict:
    report = {b: coverage_grid(field, cfg, b, cell_m) for b in bands}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    return report
