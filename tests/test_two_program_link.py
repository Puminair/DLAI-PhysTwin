"""The two-program link: the physical twin emits Layer-2 to a sink; a
separate DLAI process consumes it and the alert fires. No sockets — the
producer and consumer are exercised as objects across a real file."""
from __future__ import annotations

import json
from pathlib import Path

from config import load_config
from dlai.floorplan import FloorPlan
from dlai.ingest import load_sensor_sites
from viz.dlai_runtime import INJECTABLE, DlaiRuntime, attack_records

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def test_attack_records_cover_every_injectable():
    for aid in INJECTABLE:
        recs = attack_records(aid, 1000, x=10.0, y=10.0, mac="0c:8b:7d:00:00:01")
        assert recs, f"{aid} produced no Layer-2 record"
        assert all("stream" in r for r in recs)


def test_unknown_attack_yields_no_records():
    assert attack_records("nope", 1) == []


def _dlai():
    cfg = load_config()
    return DlaiRuntime(load_sensor_sites(DATA / "sensing_layer2.json"),
                       FloorPlan(DATA / "store_layer1.geojson"),
                       cfg.get("rules.min_aps_for_position"))


def test_twin_emits_to_sink_and_dlai_process_fires(tmp_path):
    # producer side: build the attack's Layer-2 batch and write it as the
    # twin's sink would (one JSONL line)
    batch = {"deliveredAt": 1_754_600_000_000,
             "records": attack_records("rogue_ap_on_pos_vlan", 1_754_600_000_000)}
    sink = tmp_path / "live_stream.jsonl"
    sink.write_text(json.dumps(batch) + "\n", encoding="utf-8")

    # consumer side: a separate DLAI runtime reads that line and reacts
    dlai = _dlai()
    for line in sink.read_text().splitlines():
        dlai.ingest_batch(json.loads(line))

    ids = {a["alert_id"] for a in dlai.recent_alerts}
    assert "rogue_ap_on_pos_vlan" in ids
    assert "rogue_ap_on_wire" in ids       # the POS bridge is also on the wire
    assert all(a["confidence"] for a in dlai.recent_alerts)


def test_cart_clone_stream_fires_duplicate_in_the_dlai_process(tmp_path):
    t = 1_754_600_000_000
    recs = attack_records("cart_mac_clone", t, x=25.0, y=20.0,
                          mac="0c:8b:7d:00:00:07")
    dlai = _dlai()
    dlai.ingest_batch({"deliveredAt": t, "records": recs})
    assert "cart_mac_duplicate" in {a["alert_id"] for a in dlai.recent_alerts}
