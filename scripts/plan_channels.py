"""Deterministic 2.4 GHz channel plan for the 150 logical sensor positions.

Layer: none (offline tooling; imports config only — same rule as
scripts/generate_data.py).

Assigns each logical CW9172I position in data/sensing_layer2.json one of
the three non-overlapping 20 MHz channels in the 2.4 GHz band (1 / 6 / 11
— verified: IEEE 802.11 channelisation; these are the only three
non-overlapping 20 MHz channels in 2.4 GHz).

Algorithm — greedy graph colouring, fully deterministic:
  1. Sort sensors by sensor_id (fixed processing order; the IDs are the
     generator's stable ordering, so two runs are byte-identical).
  2. For each sensor, for each channel in (1, 6, 11), compute the
     co-channel interference cost
         cost(ch) = sum( 1 / d^2 )   over already-assigned sensors on the
                                     same channel within NEIGHBOR_RADIUS_M
     with d the horizontal distance in metres (all mounts share
     z = sensors.mount_z_m, so 2-D distance is exact). Walls and fixtures
     are deliberately ignored — worst case: an interior wall only ever
     *reduces* co-channel interference, so this plan is conservative.
  3. Assign the channel with the lowest cost; ties break to the lowest
     channel number (deterministic).

The 1/d^2 weight is free-space power falloff (FSPL exponent 2) — the
relative interference power of a co-channel neighbour, dropping constant
terms that are identical for every candidate channel.

Note the min same-channel spacing this layout admits: the sales-floor
grid places 4 sensors pairwise closer than 8 m (e.g. S018/S019/S028/S029,
all pairs <= 6.64 m), so with only 3 channels some same-channel pair
closer than 8 m is unavoidable (pigeonhole). The planner minimises the
resulting cost; tests/test_channel_plan.py carries the proof.

Usage: python3 scripts/plan_channels.py [--sensors ...] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402

# The only three non-overlapping 20 MHz channels at 2.4 GHz.
# provenance: verified — IEEE 802.11 2.4 GHz channelisation (25 MHz
# spacing between centres 2412/2437/2462 MHz > 20 MHz occupied width).
CHANNELS_24GHZ: tuple[int, ...] = (1, 6, 11)

# provenance: assumed — engineering judgment. Beyond 30 m of free-space
# path (~70 dB FSPL at 2.4 GHz) a co-channel neighbour heard from a
# 24 dBm-EIRP AP (config: sensors.ap_eirp_dbm) is at or below the -67 dBm
# location threshold (config: rules.location_threshold_dbm) even before
# any obstruction loss; walls are ignored, so this is the worst case.
DEFAULT_NEIGHBOR_RADIUS_M = 30.0

# provenance: assumed — engineering judgment. Desired minimum same-channel
# spacing; reported (not enforced — see module docstring: provably
# unattainable everywhere with 3 channels on this layout).
DEFAULT_TARGET_MIN_SPACING_M = 8.0


def load_sensors(path: Path) -> list[dict]:
    """Load logical sensor positions from data/sensing_layer2.json."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    sensors = data["sensors"]
    # Fixed, reproducible processing order: sensor_id is the generator's
    # stable ordering (S000..S149).
    return sorted(sensors, key=lambda s: s["sensor_id"])


def horizontal_distance_m(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def cochannel_cost(sensor: dict, others: list[dict],
                   assignment: dict[str, int], channel: int,
                   neighbor_radius_m: float) -> float:
    """Sum of 1/d^2 over same-channel assigned sensors within radius."""
    cost = 0.0
    for other in others:
        if assignment.get(other["sensor_id"]) != channel:
            continue
        d = horizontal_distance_m(sensor, other)
        if 0.0 < d <= neighbor_radius_m:
            cost += 1.0 / (d * d)
    return cost


def plan_channels(sensors: list[dict],
                  channels: tuple[int, ...] = CHANNELS_24GHZ,
                  neighbor_radius_m: float = DEFAULT_NEIGHBOR_RADIUS_M
                  ) -> dict[str, int]:
    """Greedy colouring in fixed order; ties break to the lowest channel."""
    assignment: dict[str, int] = {}
    assigned: list[dict] = []
    for sensor in sensors:
        best_channel = None
        best_cost = math.inf
        for channel in channels:  # ascending -> lowest channel wins ties
            cost = cochannel_cost(sensor, assigned, assignment, channel,
                                  neighbor_radius_m)
            if cost < best_cost:
                best_cost = cost
                best_channel = channel
        assignment[sensor["sensor_id"]] = best_channel
        assigned.append(sensor)
    return assignment


def plan_stats(sensors: list[dict], assignment: dict[str, int],
               channels: tuple[int, ...],
               neighbor_radius_m: float) -> dict:
    """Derived stats; every number's provenance is this computation."""
    counts = {str(ch): 0 for ch in channels}
    for ch in assignment.values():
        counts[str(ch)] += 1

    min_same: float | None = None
    min_pair: list[str] = []
    for i, a in enumerate(sensors):
        for b in sensors[i + 1:]:
            if assignment[a["sensor_id"]] != assignment[b["sensor_id"]]:
                continue
            d = horizontal_distance_m(a, b)
            if min_same is None or d < min_same:
                min_same = d
                min_pair = [a["sensor_id"], b["sensor_id"]]

    per_sensor_costs = [
        cochannel_cost(s, [o for o in sensors if o is not s], assignment,
                       assignment[s["sensor_id"]], neighbor_radius_m)
        for s in sensors
    ]
    mean_cost = sum(per_sensor_costs) / len(per_sensor_costs)

    return {
        "per_channel_counts": counts,
        "min_same_channel_distance_m": round(min_same, 3),
        "min_same_channel_pair": min_pair,
        "mean_cochannel_cost_per_m2": round(mean_cost, 6),
        "max_cochannel_cost_per_m2": round(max(per_sensor_costs), 6),
    }


def build_plan(sensors_path: Path,
               neighbor_radius_m: float = DEFAULT_NEIGHBOR_RADIUS_M,
               target_min_spacing_m: float = DEFAULT_TARGET_MIN_SPACING_M
               ) -> dict:
    cfg = load_config()
    sensors = load_sensors(sensors_path)
    # sensors.logical is the v0.2 baseline; the live layout may differ once
    # reallocation / operator additions have run. Warn on a shrink (likely a
    # broken file), but plan whatever positions are actually deployed.
    expected = cfg.get("sensors.logical")
    if len(sensors) < expected:
        print(f"warning: {len(sensors)} logical sensors, fewer than the "
              f"{expected} baseline (config: sensors.logical) — planning anyway")

    assignment = plan_channels(sensors, CHANNELS_24GHZ, neighbor_radius_m)
    stats = plan_stats(sensors, assignment, CHANNELS_24GHZ,
                       neighbor_radius_m)

    return {
        "provenance": {
            "generator": "scripts/plan_channels.py",
            "algorithm": ("greedy graph colouring, fixed sensor_id order; "
                          "per sensor pick channel minimising "
                          "sum(1/d^2) over already-assigned same-channel "
                          "neighbours within neighbor_radius_m; ties break "
                          "to the lowest channel; walls ignored "
                          "(worst case); deterministic — no RNG"),
            "inputs": {
                "sensors_file": "data/sensing_layer2.json",
                "channels": list(CHANNELS_24GHZ),
                "channels_note": ("verified — the only three "
                                  "non-overlapping 20 MHz channels in "
                                  "the 2.4 GHz band"),
                "neighbor_radius_m": neighbor_radius_m,
                "neighbor_radius_note": ("assumed: engineering judgment — "
                                         "see scripts/plan_channels.py"),
                "target_min_spacing_m": target_min_spacing_m,
                "target_min_spacing_note": (
                    "assumed: engineering judgment; reported only — "
                    "provably unattainable everywhere with 3 channels "
                    "on this layout (4 sensors pairwise < 8 m)"),
            },
        },
        "band_ghz": 2.4,
        "channel_width_mhz": 20,
        "assignments": [
            {"sensor_id": s["sensor_id"], "channel": assignment[s["sensor_id"]]}
            for s in sensors
        ],
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Deterministic 2.4 GHz channel plan (channels 1/6/11) "
                    "for the 150 logical sensor positions.")
    parser.add_argument("--sensors", type=Path,
                        default=root / "data" / "sensing_layer2.json",
                        help="sensor positions JSON (default: data/sensing_layer2.json)")
    parser.add_argument("--out", type=Path,
                        default=root / "data" / "channel_plan.json",
                        help="output plan JSON (default: data/channel_plan.json)")
    parser.add_argument("--neighbor-radius-m", type=float,
                        default=DEFAULT_NEIGHBOR_RADIUS_M,
                        help="co-channel neighbour radius, metres "
                             "(assumed: engineering judgment)")
    parser.add_argument("--target-min-spacing-m", type=float,
                        default=DEFAULT_TARGET_MIN_SPACING_M,
                        help="desired same-channel spacing, metres, "
                             "reported only (assumed: engineering judgment)")
    args = parser.parse_args(argv)

    plan = build_plan(args.sensors, args.neighbor_radius_m,
                      args.target_min_spacing_m)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)
        fh.write("\n")

    stats = plan["stats"]
    print(f"wrote {args.out}")
    print(f"  per-channel counts: {stats['per_channel_counts']}")
    print(f"  min same-channel distance: "
          f"{stats['min_same_channel_distance_m']} m "
          f"({'/'.join(stats['min_same_channel_pair'])})")
    print(f"  mean co-channel cost: {stats['mean_cochannel_cost_per_m2']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
