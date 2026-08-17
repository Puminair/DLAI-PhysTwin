"""RF ↔ video coverage fusion: the four classes and the dangerous cell."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import load_config
from eval.coverage_fusion import fuse
from sensing.sensor import SensorField, load_sensors
from sensing.vision import VisionField, load_cameras
from world.geometry import StoreGeometry

DATA = Path(__file__).resolve().parents[1] / "data"
CLASSES = {"both", "rf_only", "video_only", "neither"}


@pytest.fixture(scope="module")
def report():
    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    sf = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                     geometry=geo, cfg=cfg)
    vf = VisionField(load_cameras(DATA / "cameras.json"), geo, cfg)
    # coarse grid keeps the test quick
    return fuse(sf, vf, cfg, band="2.4", cell_m=4.0)


def test_every_cell_has_one_of_the_four_classes(report):
    assert report["cells"]
    for c in report["cells"]:
        assert c["class"] in CLASSES


def test_class_counts_sum_to_cell_totals(report):
    for zone, s in report["summary"].items():
        counts = s["counts"]
        assert set(counts) == CLASSES
        assert sum(counts.values()) == s["cells"]


def test_percentages_are_consistent(report):
    for zone, s in report["summary"].items():
        assert abs(s["both"] + s["rf_only"] + s["video_only"] + s["neither"]
                   - 100.0) < 0.2 or s["cells"] == 0


def test_rf_still_dominates_the_sales_floor(report):
    # honest expected shape: RF positions almost everywhere on the sales
    # floor, so most cells are 'both' or 'rf_only', not 'video_only'
    s = report["summary"]["sales_floor"]
    assert s["both"] + s["rf_only"] > 90.0


@pytest.mark.skipif(not (DATA / "fusion_report.json").exists(),
                    reason="run scripts/build_fusion.py first")
def test_fusion_report_schema():
    doc = json.loads((DATA / "fusion_report.json").read_text())
    for key in ("band", "cell_m", "cells", "summary"):
        assert key in doc
    for zone in ("sales_floor", "back_of_house"):
        assert set(doc["summary"][zone]["counts"]) == CLASSES
