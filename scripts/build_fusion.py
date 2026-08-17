"""Precompute the RF↔video fusion report -> data/fusion_report.json.

Layer: offline tooling. Run after generate_data / reallocation so the
viz and docs read a current fused map instead of recomputing (~15 s).

Usage: python scripts/build_fusion.py [--band 2.4]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config                        # noqa: E402
from eval.coverage_fusion import write_fusion_report  # noqa: E402
from sensing.sensor import SensorField, load_sensors  # noqa: E402
from sensing.vision import VisionField, load_cameras  # noqa: E402
from world.geometry import StoreGeometry              # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", default="2.4", choices=["2.4", "5", "6"])
    args = ap.parse_args()
    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    sf = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                     geometry=geo, cfg=cfg)
    vf = VisionField(load_cameras(DATA / "cameras.json"), geo, cfg)
    report = write_fusion_report(sf, vf, cfg, DATA / "fusion_report.json",
                                 band=args.band)
    print(f"{'zone':>14} {'both':>6} {'rf_only':>8} {'video_only':>11} {'neither':>8}")
    for zone, s in report["summary"].items():
        print(f"{zone:>14} {s['both']:>6} {s['rf_only']:>8} "
              f"{s['video_only']:>11} {s['neither']:>8}  (%)")


if __name__ == "__main__":
    main()
