"""Apply the accepted reallocation proposal to the sensor layout.

Layer: offline tooling. Executes data/reallocation_proposal.json against
data/sensing_layer2.json (and the sensor list embedded in
data/store_scene3d.json): the K donor perimeter sensors are removed and
K new back-of-house positions added, keeping the locked total of 150
logical positions. The layout version bumps to v0.3-reallocated and the
provenance block records exactly which proposal was applied, so the
question "why is this sensor here" has its answer in the data.

Downstream artifacts that depend on sensor positions must be rebuilt
after this runs (the script prints the list): channel plan, coverage
report, Meraki request plan.

Usage: python scripts/apply_reallocation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA = Path(__file__).resolve().parents[1] / "data"


def main() -> None:
    with open(DATA / "reallocation_proposal.json", "r", encoding="utf-8") as fh:
        proposal = json.load(fh)
    with open(DATA / "sensing_layer2.json", "r", encoding="utf-8") as fh:
        layer2 = json.load(fh)

    donors = set(proposal["donors"])
    sensors = layer2["sensors"]
    already = {s["sensor_id"] for s in sensors}
    if not donors <= already:
        raise SystemExit(f"donors not present (already applied?): "
                         f"{sorted(donors - already)}")

    kept = [s for s in sensors if s["sensor_id"] not in donors]
    template = sensors[0]
    next_id = max(int(s["sensor_id"][1:]) for s in sensors) + 1
    added = []
    for pos in proposal["new_positions"]:
        added.append({
            "sensor_id": f"S{next_id:03d}",
            "x": round(float(pos["x"]), 2),
            "y": round(float(pos["y"]), 2),
            "z": layer2["mount_z_m"],
            "zone": pos.get("zone", "back_of_house"),
            "model": template["model"],
            "units": template["units"],
        })
        next_id += 1
    new_sensors = kept + added
    if len(new_sensors) != len(sensors):
        raise SystemExit(f"count changed: {len(sensors)} -> {len(new_sensors)}")

    layer2["sensors"] = new_sensors
    layer2["provenance"] = {
        **layer2.get("provenance", {}),
        "dataset_version": "1.0-parametric+v0.3-reallocated",
        "note": (f"applied data/reallocation_proposal.json (k={proposal['k']}): "
                 f"removed perimeter {sorted(donors)}, added BOH "
                 f"{[s['sensor_id'] for s in added]}; proven at 5 GHz "
                 f"BOH >=3AP {proposal['before']['back_of_house']['ge3ap_pct']}"
                 f"->{proposal['after']['back_of_house']['ge3ap_pct']} pct"),
        "applied_by": "scripts/apply_reallocation.py",
    }
    with open(DATA / "sensing_layer2.json", "w", encoding="utf-8") as fh:
        json.dump(layer2, fh, indent=1)

    # keep the 3D scene's embedded sensor list in sync — the viz reads it
    with open(DATA / "store_scene3d.json", "r", encoding="utf-8") as fh:
        scene = json.load(fh)
    scene["sensors"] = new_sensors
    scene["provenance"]["note"] = (scene["provenance"].get("note", "")
                                   + " | sensors: v0.3-reallocated")
    with open(DATA / "store_scene3d.json", "w", encoding="utf-8") as fh:
        json.dump(scene, fh, indent=1)

    print(f"applied: -{len(donors)} perimeter, +{len(added)} back_of_house "
          f"({[s['sensor_id'] for s in added]})")
    print("rebuild now: scripts/plan_channels.py, scripts/build_coverage.py, "
          "cisco/meraki_provision.py --dry-run")


if __name__ == "__main__":
    main()
