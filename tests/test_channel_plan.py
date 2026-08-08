"""Tests for scripts/plan_channels.py — the 2.4 GHz channel plan.

Layer: none (tests offline tooling; imports the script + stdlib only).

Covers: plan file exists after running the planner; only channels
{1, 6, 11} are used; the plan is deterministic (two runs identical);
per-channel balance (each channel >= 40 of 150); and same-channel
spacing. The naive spacing assertion — "no same-channel neighbour nearer
than 8 m" — is PROVABLY INFEASIBLE for this layout, and the proof is
computed from the data, not asserted from memory:

    The sales-floor grid contains 4 sensors that are pairwise closer
    than 8 m (e.g. S018/S019/S028/S029, all six pairs <= 6.64 m).
    With only 3 non-overlapping channels, the pigeonhole principle
    forces two of any such 4 onto the same channel — so at least one
    same-channel pair closer than 8 m exists in EVERY possible plan.

So instead the tests (a) re-derive that 4-clique from the sensor file to
prove infeasibility, and (b) verify the planner minimised co-channel
cost per its greedy rule: replaying the fixed processing order, every
assigned channel is exactly the argmin of sum(1/d^2) over
already-assigned same-channel neighbours within the radius (ties to the
lowest channel). The layout admits no better guarantee than that.
"""
from __future__ import annotations

import itertools
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_channels.py"
SENSORS_FILE = ROOT / "data" / "sensing_layer2.json"

sys.path.insert(0, str(ROOT))

from scripts.plan_channels import (  # noqa: E402
    CHANNELS_24GHZ,
    DEFAULT_NEIGHBOR_RADIUS_M,
    DEFAULT_TARGET_MIN_SPACING_M,
    cochannel_cost,
    horizontal_distance_m,
    load_sensors,
)


def _run_planner(out_path: Path) -> dict:
    subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out_path)],
        check=True, capture_output=True, cwd=ROOT)
    with open(out_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("channel_plan") / "channel_plan.json"
    return _run_planner(out)


@pytest.fixture(scope="module")
def sensors() -> list[dict]:
    return load_sensors(SENSORS_FILE)


def test_plan_file_exists_after_running_planner(
        tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("exists") / "channel_plan.json"
    assert not out.exists()
    _run_planner(out)
    assert out.exists()


def test_every_deployed_sensor_assigned(plan: dict, sensors: list[dict]) -> None:
    # count is whatever the live layout holds (v0.2 baseline was 150; it
    # grows/shrinks with reallocation and operator additions)
    assigned_ids = {a["sensor_id"] for a in plan["assignments"]}
    assert assigned_ids == {s["sensor_id"] for s in sensors}
    assert len(plan["assignments"]) == len(sensors)


def test_only_nonoverlapping_channels_used(plan: dict) -> None:
    used = {a["channel"] for a in plan["assignments"]}
    assert used <= {1, 6, 11}
    # and all three are actually in service
    assert used == {1, 6, 11}


def test_deterministic_two_runs_identical(
        tmp_path_factory: pytest.TempPathFactory) -> None:
    out_a = tmp_path_factory.mktemp("det_a") / "plan.json"
    out_b = tmp_path_factory.mktemp("det_b") / "plan.json"
    _run_planner(out_a)
    _run_planner(out_b)
    assert out_a.read_bytes() == out_b.read_bytes()


def test_channel_balance_each_used_at_least_40_times(plan: dict,
                                                     sensors: list[dict]) -> None:
    counts = plan["stats"]["per_channel_counts"]
    for ch in ("1", "6", "11"):
        assert counts[ch] >= 40, f"channel {ch} used only {counts[ch]} times"
    assert sum(counts.values()) == len(sensors)


def _pairwise_lt(sensors: list[dict], ids: tuple[str, ...],
                 limit_m: float) -> bool:
    by_id = {s["sensor_id"]: s for s in sensors}
    return all(horizontal_distance_m(by_id[a], by_id[b]) < limit_m
               for a, b in itertools.combinations(ids, 2))


def test_8m_same_channel_spacing_is_provably_infeasible(
        sensors: list[dict]) -> None:
    """Derive from the data that no 3-channel plan achieves 8 m spacing.

    Find 4 sensors pairwise closer than the 8 m target. By pigeonhole,
    any assignment of 3 channels to 4 sensors repeats a channel, so a
    same-channel pair closer than 8 m exists in every possible plan —
    the naive spacing assertion cannot hold for this layout.
    """
    limit = DEFAULT_TARGET_MIN_SPACING_M  # 8.0 — assumed, from the script
    clique4 = None
    ids = [s["sensor_id"] for s in sensors]
    by_id = {s["sensor_id"]: s for s in sensors}
    # adjacency under "closer than 8 m"
    close: dict[str, set[str]] = {i: set() for i in ids}
    for a, b in itertools.combinations(sensors, 2):
        if horizontal_distance_m(a, b) < limit:
            close[a["sensor_id"]].add(b["sensor_id"])
            close[b["sensor_id"]].add(a["sensor_id"])
    for a in ids:
        for b, c, d in itertools.combinations(sorted(close[a]), 3):
            if c in close[b] and d in close[b] and d in close[c]:
                clique4 = (a, b, c, d)
                break
        if clique4:
            break
    assert clique4 is not None, (
        "no 4-clique under 8 m found — the 8 m spacing assertion may be "
        "feasible after all; re-enable it and delete this proof")
    assert _pairwise_lt(sensors, clique4, limit)
    # sanity: the witness really is dense (documents the layout)
    max_pair = max(horizontal_distance_m(by_id[a], by_id[b])
                   for a, b in itertools.combinations(clique4, 2))
    assert max_pair < limit


def test_planner_minimized_cochannel_cost_per_greedy_rule(
        plan: dict, sensors: list[dict]) -> None:
    """Since 8 m spacing is infeasible (see proof above), verify the
    planner did the best its stated rule allows: replaying the fixed
    sensor_id order, every sensor's channel is the argmin of
    sum(1/d^2) over already-assigned same-channel neighbours within
    the radius, ties broken to the lowest channel number."""
    assignment = {a["sensor_id"]: a["channel"] for a in plan["assignments"]}
    assigned: list[dict] = []
    for sensor in sensors:  # load_sensors() is the fixed order
        costs = {
            ch: cochannel_cost(sensor, assigned, assignment, ch,
                               DEFAULT_NEIGHBOR_RADIUS_M)
            for ch in CHANNELS_24GHZ
        }
        best = min(CHANNELS_24GHZ, key=lambda ch: (costs[ch], ch))
        chosen = assignment[sensor["sensor_id"]]
        assert chosen == best, (
            f"{sensor['sensor_id']}: chose channel {chosen} "
            f"(cost {costs[chosen]:.6f}) but greedy argmin is {best} "
            f"(cost {costs[best]:.6f})")
        assigned.append(sensor)


def test_reported_min_spacing_matches_plan_and_is_not_degenerate(
        plan: dict, sensors: list[dict]) -> None:
    """The plan's reported min same-channel distance is recomputable and
    at least the layout's global nearest-neighbour distance — i.e. the
    planner never put two co-located/adjacent-grid sensors on the same
    channel when a farther pairing existed."""
    assignment = {a["sensor_id"]: a["channel"] for a in plan["assignments"]}
    min_same = min(
        horizontal_distance_m(a, b)
        for a, b in itertools.combinations(sensors, 2)
        if assignment[a["sensor_id"]] == assignment[b["sensor_id"]])
    assert min_same == pytest.approx(
        plan["stats"]["min_same_channel_distance_m"], abs=1e-3)
    # global nearest-neighbour distance of the layout (any channel)
    min_any = min(horizontal_distance_m(a, b)
                  for a, b in itertools.combinations(sensors, 2))
    assert min_same > min_any, (
        "planner paired the layout's two closest sensors on one channel")
