"""Capture a pure Layer-2 observation stream for the live view.

Layer: offline tooling (may import world + sensing, exactly like eval/).
The OUTPUT is Layer-2 only — the delivered observation batches, the same
JSON a real Meraki Scanning API v3 deployment emits — with no world ids
in it (verified by tests/test_sensing.py). The live server replays this
through Layer 3 alone, with no access to ground truth, which is the whole
point: it is the blind data path driving a live picture.

In a real deployment you would instead point the live server at the
JSONL sink written by cisco/scanning_receiver.py. This script exists so
the live view can be demonstrated without hardware.

Each output line is one ObservationBatch: {"deliveredAt": ms, "records": [...]}.

Usage: python scripts/capture_observations.py --minutes 20 [--out data/capture_layer2.jsonl]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config                        # noqa: E402
from sensing.aireye import AirEyeEmulator             # noqa: E402
from sensing.pipeline import ObservationPipeline      # noqa: E402
from sensing.sensor import SensorField, load_sensors  # noqa: E402
from world.geometry import StoreGeometry              # noqa: E402
from world.sim import WorldSim                        # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--observe-every", type=float, default=5.0)
    ap.add_argument("--arrival-rate", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(DATA / "capture_layer2.jsonl"))
    args = ap.parse_args(argv)

    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                        geometry=geo, cfg=cfg)
    sim = WorldSim(geometry=geo, cfg=cfg, seed=args.seed,
                   arrival_rate_per_min=args.arrival_rate)
    pipe = ObservationPipeline(field_=field, cfg=cfg)
    aireye = AirEyeEmulator(seed=args.seed)

    dt = 0.5
    obs_every = max(1, int(args.observe_every / dt))
    steps = int(args.minutes * 60 / dt)
    for i in range(steps):
        sim.step(dt)
        if i % obs_every == obs_every - 1:
            pipe.observe(sim.snapshot())
    pipe.flush_all(sim.clock.now_ms())

    # weave the security streams (Air Marshal + aWIPS) into the delivered
    # batches so the live view exercises the alert engine too. These are
    # Layer-2 records like any other — the emulator lives in sensing/.
    if pipe.delivered:
        first_t = pipe.delivered[0].delivered_at_ms
        pipe.delivered[0].records.extend(aireye.neighbour_records(first_t))
        mid = len(pipe.delivered) // 2
        pipe.delivered[mid].records.append(
            aireye.rogue_on_wire_record(pipe.delivered[mid].delivered_at_ms))
        # a throttled deauth burst across a couple of APs
        ap = sorted(field.units, key=lambda u: u.sensor_id)[0].mac
        for b in pipe.delivered[mid:mid + 3]:
            ev = aireye.awips_event("deauth_flood", ap, b.delivered_at_ms)
            if ev is not None:
                b.records.append(ev)

    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as fh:
        for batch in pipe.delivered:
            fh.write(json.dumps(batch.to_json()) + "\n")
    n_rec = sum(len(b.records) for b in pipe.delivered)
    # provenance sidecar so nobody mistakes this capture for real hardware
    with open(out.with_suffix(".meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": "scripts/capture_observations.py",
                   "note": "Layer-2 only; no ground truth; replay through "
                           "Layer 3 for the live view",
                   "minutes": args.minutes, "seed": args.seed,
                   "batches": len(pipe.delivered), "records": n_rec}, fh, indent=1)
    print(f"wrote {out} — {len(pipe.delivered)} batches, {n_rec} records")


if __name__ == "__main__":
    main()
