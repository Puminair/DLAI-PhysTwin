"""Propose moving K perimeter sensors into back-of-house coverage gaps.

Layer: offline tooling (like scripts/build_coverage.py). May import
config, world.geometry, sensing.sensor, eval.coverage.

Motivation (CLAUDE.md §8/§9, README coverage table): BOH has 28 logical
sensors over 2,218 m². At 5/6 GHz only ~66 % of BOH cells hear >=3 APs
and ~17-18 % are fully blind. "No physics fixes it, only reallocation
does." Meanwhile the 18 perimeter units cannot separate inside from
outside (§9: a modem 8 m out is heard by 41 sensors at -44 dBm), so
they are the natural donor pool.

Method:
  1. Baseline: eval.coverage.coverage_grid at band "5", 2 m cells.
  2. Candidates: BOH open cells currently failing the 3-AP rule,
     filtered so no fixture occupies the mount point (geometry.fixtures_at
     at mount height rejects racking / cold rooms / columns).
  3. Greedy placement WITHOUT per-step re-simulation: score a candidate
     by how many currently-deficient cells lie within REACH_M and are
     "line-of-sight-ish" from mount z=3.0 to panel z=1.0 (crossing set
     contains no cold_room and <=1 racking). This is a HEURISTIC — see
     provenance notes at the constants below. The claim is then PROVED
     by re-running the repo's own physics (coverage_grid) on the
     modified sensor list; only those re-run numbers are reported.
  4. Donors: the K perimeter units on the sales side (x < x_split),
     preferring west/south walls farthest from the split wall. See
     select_donors() for the justification.

Usage: python3 scripts/propose_reallocation.py [--k 8] [--band 5]
Runtime: ~2 coverage_grid runs (~10 s each) + a cheap heuristic loop.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config                          # noqa: E402
from eval.coverage import coverage_grid                 # noqa: E402
from sensing.sensor import SensorField, SensorUnit, load_sensors  # noqa: E402
from world.geometry import StoreGeometry                # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"

# ---------------------------------------------------------------------------
# Heuristic constants — all `assumed`, overridable via argparse (CLAUDE.md §10).
#
# REACH_M = 12.0        assumed — planning radius for the greedy score. At
#     5 GHz a clear path budgets 16 (cart EIRP, config cart_uplink) + 5
#     (antenna gain) + 2.5 (diversity) - FSPL(12 m, 5500 MHz ~ 68.8 dB)
#     = -45 dBm, ~22 dB above the -67 dBm location threshold
#     (rules.location_threshold_dbm), so a clear cell within 12 m is
#     safely heard. NOTE: with one racking crossing (33.4 dB at 5 GHz,
#     attenuation_db in config/defaults.yaml) the same budget only
#     closes within ~3 m, so the <=1-racking reach rule below is
#     OPTIMISTIC for racking-crossed cells. That is acceptable: the
#     score only orders candidates; the reported effect comes from the
#     full physics re-run.
# MAX_RACKING_CROSSINGS = 1   assumed — per task spec; see optimism note.
# ---------------------------------------------------------------------------
DEFAULT_K = 8
DEFAULT_REACH_M = 12.0
MAX_RACKING_CROSSINGS = 1

# Materials that must not occupy a mount point. Derived from the solid
# set used by eval.coverage.coverage_grid plus chiller (a sensor cannot
# hang inside a chiller cabinet either).
SOLID_MATERIALS = frozenset(
    {"gondola", "column", "cold_room", "racking", "wall", "room", "chiller"})


def is_valid_candidate(geo: StoreGeometry, x: float, y: float,
                       mount_z_m: float) -> bool:
    """True if a sensor can be mounted at (x, y, mount_z_m).

    Rejects points outside the envelope and points where any solid
    fixture occupies the mount height — racking (z1 = 4.0 m) and cold
    rooms (z1 = 3.5 m) both extend above the 3.0 m mount plane, so
    fixtures_at at mount height catches exactly the fixtures the task
    cares about (racking, cold rooms, columns, walls).
    """
    if not geo.inside_envelope(x, y):
        return False
    return not any(f.material in SOLID_MATERIALS
                   for f in geo.fixtures_at(x, y, mount_z_m))


def select_donors(units: list[SensorUnit], k: int,
                  x_split: float) -> list[SensorUnit]:
    """Pick the K perimeter units whose removal least harms coverage.

    Justification (heuristic, not a re-simulation):
      * Only zone == "perimeter" units are eligible — §9 says they do
        not achieve their stated purpose (inside/outside discrimination
        does not work), so they are the donor pool.
      * Only sales-side units (x < x_split) are eligible. Perimeter
        units east of the split wall sit over the SPARSE back-of-house
        field and are among the few sensors BOH cells can hear;
        removing them would worsen the very zone we are fixing.
      * Among sales-side units, prefer the west/south walls farthest
        from the split wall: the sales floor behind them holds 104
        sensors over 2,782 m² (one per ~27 m², config sensors.sales_units),
        so edge cells there keep >=3 candidate APs without perimeter
        help — the README/data show sales-floor >=3-AP at 99.5 % in
        every band, a figure the perimeter ring barely contributes to.
        North-wall sales-side units are the fallback when K exceeds the
        west/south pool, ordered by the same distance-from-split key.
    """
    def wall_of(u: SensorUnit) -> str:
        # derived from data/sensing_layer2.json placements: perimeter
        # units sit within ~1 m of their wall.
        if u.x < 5.0:
            return "west"
        if u.y < 5.0:
            return "south"
        if u.y > 50.0:
            return "north"
        return "east"

    pool = [u for u in units if u.zone == "perimeter" and u.x < x_split]
    if k > len(pool):
        raise ValueError(
            f"k={k} exceeds the {len(pool)} sales-side perimeter units")
    pool.sort(key=lambda u: (0 if wall_of(u) in ("west", "south") else 1,
                             -(x_split - u.x)))
    return pool[:k]


def _make_unit(i: int, x: float, y: float, z: float) -> SensorUnit:
    """New logical BOH position. IDs continue the S-numbering past 149;
    MAC follows the same scheme as sensing.sensor.load_sensors."""
    n = 150 + i
    return SensorUnit(
        sensor_id=f"S{n}", x=round(x, 2), y=round(y, 2), z=z,
        zone="back_of_house",
        mac=f"00:2a:10:{(n >> 8) & 0xFF:02x}:{n & 0xFF:02x}:00")


def greedy_new_positions(geo: StoreGeometry, failing_cells: list[dict],
                         k: int, reach_m: float, mount_z_m: float,
                         panel_z_m: float,
                         min_aps: int) -> list[tuple[float, float]]:
    """Greedy placement over failing BOH cells, no re-simulation.

    HEURISTIC (provenance: this script, task spec). A candidate scores
    the number of still-deficient cells within reach_m whose crossing
    set (sensor z=3.0 -> cell z=1.0, world.geometry.segment_crossings)
    contains no cold_room and <=1 racking. Refinement over pure
    set-cover: each cell carries deficit = min_aps - n_aps; a chosen
    sensor decrements the deficit of every cell it reaches, so a blind
    cell (deficit 3) keeps attracting sensors until three reach it —
    closer to the physics than marking it solved after one.
    """
    cands = [(c["x"], c["y"]) for c in failing_cells
             if is_valid_candidate(geo, c["x"], c["y"], mount_z_m)]
    deficit = [max(0, min_aps - c["n_aps"]) for c in failing_cells]
    cells_xy = [(c["x"], c["y"]) for c in failing_cells]

    # Precompute reach sets once — the greedy loop is then trivial.
    reach: list[list[int]] = []
    for cx, cy in cands:
        got: list[int] = []
        for j, (fx, fy) in enumerate(cells_xy):
            if math.hypot(fx - cx, fy - cy) > reach_m:
                continue
            crossings = geo.segment_crossings(
                (cx, cy, mount_z_m), (fx, fy, panel_z_m))
            if (crossings.get("cold_room", 0) == 0
                    and crossings.get("racking", 0) <= MAX_RACKING_CROSSINGS):
                got.append(j)
        reach.append(got)

    chosen: list[tuple[float, float]] = []
    used: set[int] = set()
    for _ in range(k):
        best_i, best_score = -1, -1
        for i, got in enumerate(reach):
            if i in used:
                continue
            score = sum(1 for j in got if deficit[j] > 0)
            if score > best_score:
                best_i, best_score = i, score
        if best_i < 0 or best_score <= 0:
            break                       # nothing left worth placing
        used.add(best_i)
        chosen.append(cands[best_i])
        for j in reach[best_i]:
            if deficit[j] > 0:
                deficit[j] -= 1
    return chosen


def _fmt_row(label: str, s: dict) -> str:
    return (f"{label:>22} {s['cells']:>6} {s['ge3ap_pct']:>8.1f} "
            f"{s['blind_pct']:>8.1f}")


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k", type=int, default=DEFAULT_K,
                    help="perimeter sensors to reallocate (assumed default 8)")
    ap.add_argument("--band", default="5",
                    help="band for the proof runs (default 5 — the failing one)")
    ap.add_argument("--cell", type=float, default=2.0,
                    help="grid cell size m (matches README/coverage_report)")
    ap.add_argument("--reach", type=float, default=DEFAULT_REACH_M,
                    help="heuristic planning radius m (assumed; see header)")
    ap.add_argument("--out", default=str(DATA / "reallocation_proposal.json"))
    args = ap.parse_args(argv)

    cfg = load_config()
    x_split = cfg.get("areas.x_split_m")
    mount_z = cfg.get("sensors.mount_z_m")
    panel_z = cfg.get("heights.cart_panel_z_m")
    min_aps = cfg.get("rules.min_aps_for_position")

    geo = StoreGeometry(DATA / "store_layer1.geojson")
    units = load_sensors(DATA / "sensing_layer2.json")
    field = SensorField(units=units, geometry=geo, cfg=cfg)

    t0 = time.time()
    print(f"baseline coverage_grid, band {args.band} ...", flush=True)
    before = coverage_grid(field, cfg, args.band, args.cell)
    print(f"  done in {time.time() - t0:.1f} s")

    boh_failing = [c for c in before["cells"]
                   if c["x"] > x_split and c["n_aps"] < min_aps]
    print(f"BOH cells failing 3-AP rule at {args.band} GHz: "
          f"{len(boh_failing)} of "
          f"{before['summary']['back_of_house']['cells']}")

    donors = select_donors(units, args.k, x_split)
    new_xy = greedy_new_positions(geo, boh_failing, args.k, args.reach,
                                  mount_z, panel_z, min_aps)
    new_units = [_make_unit(i, x, y, mount_z)
                 for i, (x, y) in enumerate(new_xy)]

    donor_ids = {u.sensor_id for u in donors}
    modified = [u for u in units if u.sensor_id not in donor_ids] + new_units
    assert len(modified) == len(units) - len(donors) + len(new_units)

    # The PROOF: rerun the repo's own physics on the modified field.
    field_after = SensorField(units=modified, geometry=geo, cfg=cfg)
    t0 = time.time()
    print(f"post-reallocation coverage_grid, band {args.band} ...", flush=True)
    after = coverage_grid(field_after, cfg, args.band, args.cell)
    print(f"  done in {time.time() - t0:.1f} s")

    proposal = {
        "provenance": {
            "generator": "scripts/propose_reallocation.py",
            "method": ("greedy reach heuristic (12 m, no cold_room, <=1 "
                       "racking) for placement; effect PROVED by full "
                       "eval.coverage.coverage_grid re-run on the modified "
                       "sensor list — before/after below are those re-runs"),
            "band": args.band,
            "cell_m": args.cell,
            "reach_m": args.reach,
            "baseline_dataset": "data/sensing_layer2.json (1.0-parametric)",
        },
        "k": args.k,
        "donors": sorted(donor_ids),
        "new_positions": [{"sensor_id": u.sensor_id, "x": u.x, "y": u.y,
                           "z": u.z, "zone": u.zone} for u in new_units],
        "before": before["summary"],
        "after": after["summary"],
    }
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(proposal, fh, indent=2)
    print(f"wrote {out}")

    print(f"\n{'':>22} {'cells':>6} {'>=3AP %':>8} {'blind %':>8}")
    for label, summ in (("BEFORE", before["summary"]),
                        ("AFTER", after["summary"])):
        for zone in ("back_of_house", "sales_floor"):
            print(_fmt_row(f"{label} {zone}", summ[zone]))
    print(f"\ndonors ({args.k} perimeter units, sales side): "
          f"{', '.join(sorted(donor_ids))}")
    print("new BOH positions: "
          + ", ".join(f"({u.x:.1f},{u.y:.1f})" for u in new_units))
    return proposal


if __name__ == "__main__":
    main()
