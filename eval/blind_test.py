"""The blind test — compare DLAI output against world ground truth.

May import: everything. This is the only place the two sides meet.

1. Run the world for N hours, recording ground truth.
2. Feed ONLY Layer-2 observation batches to Layer 3.
3. Compare: position error, detection precision/recall, flicker,
   per-zone breakdown. Back of house will be worst — 28 sensors over
   2,218 m² — and no physics fixes it; only reallocation does.

Usage: python -m eval.blind_test [--hours 0.25] [--observe-every 5.0]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from config import load_config
from dlai.entity import EntityResolver
from dlai.floorplan import FloorPlan
from dlai.ingest import load_sensor_sites, normalise_batch
from dlai.interaction import classify_track
from dlai.policy import PolicyEngine
from eval.metrics import (PositionErrorStats, detection_prf, flicker_rate,
                          position_errors)
from sensing.pipeline import ObservationPipeline
from sensing.sensor import SensorField, load_sensors
from world.geometry import StoreGeometry
from world.sim import WorldSim

DATA = Path(__file__).resolve().parents[1] / "data"


def run_blind_test(hours: float = 0.25, observe_every_s: float = 5.0,
                   dt_s: float = 0.5, seed: int = 7) -> dict:
    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                        geometry=geo, cfg=cfg)
    sim = WorldSim(geometry=geo, cfg=cfg, seed=seed)
    pipe = ObservationPipeline(field_=field, cfg=cfg)

    # ---- phase 1+2: run the world, record truth, observe through L2
    truth: dict[str, dict[int, tuple[float, float]]] = defaultdict(dict)
    steps = int(hours * 3600 / dt_s)
    obs_every = max(1, int(observe_every_s / dt_s))
    for i in range(steps):
        sim.step(dt_s)
        if i % obs_every == obs_every - 1:
            snap = sim.snapshot()
            for c in snap["carts"]:
                if c["state"] != "docked":
                    truth[c["id"]][snap["t_ms"]] = (c["x"], c["y"])
            pipe.observe(snap)
    pipe.flush_all(sim.clock.now_ms())

    # ---- phase 3: Layer 3 sees batches only
    sites = load_sensor_sites(DATA / "sensing_layer2.json")
    plan = FloorPlan(DATA / "store_layer1.geojson")
    resolver = EntityResolver(sites, plan,
                              min_aps=cfg.get("rules.min_aps_for_position"))
    for batch in pipe.delivered:
        for obs in normalise_batch(batch.to_json()):
            resolver.consume(obs)

    policy = PolicyEngine()
    for track in resolver.tracks.values():
        policy.evaluate_track(track, classify_track(track))

    # ---- phase 4: score. truth_links is eval-only by charter.
    links = pipe.truth_links()                 # world id -> MAC
    x_split = cfg.get("areas.x_split_m")
    per_zone_err: dict[str, list[float]] = {"sales_floor": [],
                                            "back_of_house": []}
    all_err: list[float] = []
    detected = 0
    for wid, t_xy in truth.items():
        mac = links.get(wid)
        track = resolver.tracks.get(mac) if mac else None
        if track is None or not track.estimates:
            continue
        if wid.startswith("cart_"):
            detected += 1
        errs = position_errors(t_xy, track.estimates)
        all_err.extend(errs)
        for est in track.estimates:
            zone = "sales_floor" if est.x < x_split else "back_of_house"
            e = position_errors(t_xy, [est])
            per_zone_err[zone].extend(e)

    cart_ids = {wid for wid in truth if wid.startswith("cart_")}
    truth_macs = set(links.values())
    false_tracks = sum(1 for mac in resolver.tracks if mac not in truth_macs)
    total_est = sum(len(t.estimates) for t in resolver.tracks.values())
    total_flicker = sum(t.flicker_transitions for t in resolver.tracks.values())

    report = {
        "hours": hours,
        "observed_every_s": observe_every_s,
        "detection": detection_prf(len(cart_ids), detected, false_tracks),
        "position_error_m": vars(PositionErrorStats.from_errors(all_err))
        if all_err else None,
        "position_error_by_zone": {
            z: (vars(PositionErrorStats.from_errors(e)) if e else None)
            for z, e in per_zone_err.items()},
        "flicker": {"transitions": total_flicker,
                    "per_estimate": flicker_rate(total_flicker, total_est)},
        "recommendations": len(policy.recommendations),
        "note": "back of house is expected worst; only reallocation fixes it",
    }
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0.25)
    ap.add_argument("--observe-every", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    report = run_blind_test(hours=args.hours, observe_every_s=args.observe_every)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
