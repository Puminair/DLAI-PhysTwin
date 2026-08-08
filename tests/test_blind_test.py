"""End-to-end: the blind test runs and produces sane numbers."""
from __future__ import annotations

import pytest

from eval.blind_test import run_blind_test


@pytest.fixture(scope="module")
def report():
    return run_blind_test(hours=0.1, observe_every_s=5.0, seed=7)


def test_blind_test_detects_carts(report):
    det = report["detection"]
    assert det["true_present"] > 0
    assert det["recall"] >= 0.8, "carts broadcasting at 2.4 should be found"
    assert det["false_tracks"] == 0 or det["precision"] >= 0.8


def test_position_error_is_bounded(report):
    err = report["position_error_m"]
    assert err is not None
    assert err["mean_m"] < 6.0, (
        "mean error blew past ranging plausibility — elimination broken?")


def test_report_shape_is_per_zone(report):
    assert set(report["position_error_by_zone"]) == {"sales_floor",
                                                     "back_of_house"}
    assert "flicker" in report
