"""Add Meraki-managed CW9172I sensor units to the live layout.

Layer: offline tooling. Appends new logical sensor positions (each a
pair of CW9172I units, Meraki-managed) to data/sensing_layer2.json and
the scene's embedded sensor list, keeping ids unique and stamping
provenance. Positions are validated against geometry: a point inside a
cold room, column, racking or outside the envelope is refused (a mount
cannot hang there). The layout version records how many were added and
why.

Downstream artifacts must be rebuilt afterwards (the script prints the
list): channel plan, coverage report, Meraki request plan.

Usage:
  python scripts/add_sensors.py --at 96.3 47.4 --at 97.2 36.2 \
      --reason "fill NE warehouse cold-room-shadow gap"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config                      # noqa: E402
from world.geometry import StoreGeometry            # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
_BLOCKING = {"cold_room", "column", "racking", "wall", "room"}


def valid_mount(geo: StoreGeometry, x: float, y: float, z: float) -> str | None:
    """Return a rejection reason, or None if the point is a valid mount."""
    if not geo.inside_envelope(x, y):
        return "outside envelope"
    for fx in geo.fixtures_at(x, y, z):
        if fx.material in _BLOCKING:
            return f"inside {fx.material} ({fx.fid})"
    return None


def snap_to_valid(geo: StoreGeometry, x: float, y: float, z: float,
                  radius: float = 4.0, step: float = 0.5) -> tuple[float, float]:
    """Nudge to the nearest valid mount point on a small spiral search."""
    if valid_mount(geo, x, y, z) is None:
        return x, y
    best = None
    r = step
    while r <= radius:
        n = max(8, int(2 * 3.14159 * r / step))
        for i in range(n):
            ang = 2 * 3.14159 * i / n
            cx, cy = x + r * __import__("math").cos(ang), y + r * __import__("math").sin(ang)
            if valid_mount(geo, cx, cy, z) is None:
                d = (cx - x) ** 2 + (cy - y) ** 2
                if best is None or d < best[0]:
                    best = (d, cx, cy)
        if best is not None:
            return round(best[1], 2), round(best[2], 2)
        r += step
    raise SystemExit(f"no valid mount point within {radius} m of ({x},{y})")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", nargs=2, type=float, action="append", metavar=("X", "Y"),
                    required=True, help="target x y (metres); repeatable")
    ap.add_argument("--zone", default="back_of_house")
    ap.add_argument("--reason", default="operator-added Meraki unit")
    args = ap.parse_args(argv)

    cfg = load_config()
    mount_z = cfg.get("sensors.mount_z_m")
    geo = StoreGeometry(DATA / "store_layer1.geojson")

    with open(DATA / "sensing_layer2.json", "r", encoding="utf-8") as fh:
        layer2 = json.load(fh)
    sensors = layer2["sensors"]
    template = sensors[0]
    next_id = max(int(s["sensor_id"][1:]) for s in sensors) + 1

    added = []
    for x, y in args.at:
        sx, sy = snap_to_valid(geo, x, y, mount_z)
        added.append({
            "sensor_id": f"S{next_id:03d}",
            "x": sx, "y": sy, "z": mount_z,
            "zone": args.zone, "model": template["model"],
            "units": template["units"],
            "managed_by": "meraki_dashboard",
            "provenance": args.reason,
        })
        print(f"  + {added[-1]['sensor_id']} at ({sx},{sy}) "
              f"{'(snapped)' if (sx, sy) != (x, y) else ''}")
        next_id += 1

    layer2["sensors"] = sensors + added
    prov = layer2.get("provenance", {})
    base = prov.get("dataset_version", "1.0-parametric")
    already = base.count("+add")
    prov["dataset_version"] = f"{base}+add{len(added)}"
    prov["note"] = (prov.get("note", "") +
                    f" | added {len(added)} Meraki-managed units "
                    f"({[s['sensor_id'] for s in added]}): {args.reason}")
    prov["applied_by"] = "scripts/add_sensors.py"
    layer2["provenance"] = prov
    with open(DATA / "sensing_layer2.json", "w", encoding="utf-8") as fh:
        json.dump(layer2, fh, indent=1)

    with open(DATA / "store_scene3d.json", "r", encoding="utf-8") as fh:
        scene = json.load(fh)
    scene["sensors"] = layer2["sensors"]
    scene["provenance"]["note"] = (scene["provenance"].get("note", "")
                                   + f" | +{len(added)} Meraki units")
    with open(DATA / "store_scene3d.json", "w", encoding="utf-8") as fh:
        json.dump(scene, fh, indent=1)

    total = len(layer2["sensors"])
    print(f"layout now {total} logical positions "
          f"({total * template['units']} CW9172I units)")
    print("rebuild now: scripts/plan_channels.py, scripts/build_coverage.py, "
          "cisco/meraki_provision.py --dry-run")


if __name__ == "__main__":
    main()
