"""Precompute the coverage report for all bands -> data/coverage_report.json.

Layer: offline tooling. Run once (~30 s); viz/server.py and the README
table read the result instead of recomputing.

Usage: python scripts/build_coverage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config                      # noqa: E402
from eval.coverage import write_coverage_report     # noqa: E402
from sensing.sensor import SensorField, load_sensors  # noqa: E402
from world.geometry import StoreGeometry            # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


def main() -> None:
    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                        geometry=geo, cfg=cfg)
    report = write_coverage_report(field, cfg, DATA / "coverage_report.json")
    print(f"{'band':>5} {'zone':>14} {'>=3AP %':>8} {'blind %':>8}")
    for band, r in report.items():
        for zone, s in r["summary"].items():
            print(f"{band:>5} {zone:>14} {s['ge3ap_pct']:>8} {s['blind_pct']:>8}")


if __name__ == "__main__":
    main()
