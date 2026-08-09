"""Live view: Layer-3 inference over a Layer-2 stream, no ground truth.

The live server must consume observations only and never touch world/ or
sensing/ — otherwise it is peeking at the truth it claims to infer.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from viz.live_server import LiveServer

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_live_server_imports_no_ground_truth():
    # the live branch has no Layer 1 and does not run the sensing model
    for mod in _imports(ROOT / "viz" / "live_server.py"):
        top = mod.split(".")[0]
        assert top not in ("world", "sensing"), (
            f"live_server imports {mod} — it would be peeking at the truth")


def test_ingest_batch_resolves_positions_as_inferences():
    ls = LiveServer(source=DATA / "does_not_exist.jsonl")
    # craft a batch heard by several real sensors so a fix is possible
    sites = list(ls.sites.values())[:5]
    batch = {"deliveredAt": 1000, "records": [{
        "stream": "scanning_api_v3/DevicesSeen",
        "clientMac": "0c:8b:7d:00:00:99", "seenTime": 900,
        "locations": [], "manufacturer": "RetailPanel Ltd",
        "rssiRecords": [{"apMac": s.ap_mac, "rssi": 55 - i * 3}
                        for i, s in enumerate(sites)],
        "gap": []}]}
    ls._ingest_batch(batch)
    t = ls.tracks["0c:8b:7d:00:00:99"]
    assert t["located"] is True
    assert t["confidence"] == "inferred", "a live position is never observed"
    assert "x" in t and "variance" in t
    assert ls.stats["positioned"] == 1


def test_below_min_aps_is_unlocated_not_invented():
    ls = LiveServer(source=DATA / "none.jsonl")
    site = next(iter(ls.sites.values()))
    batch = {"deliveredAt": 1, "records": [{
        "stream": "scanning_api_v3/DevicesSeen",
        "clientMac": "0c:8b:7d:00:00:98", "seenTime": 1, "locations": [],
        "manufacturer": "x",
        "rssiRecords": [{"apMac": site.ap_mac, "rssi": 50}], "gap": []}]}
    ls._ingest_batch(batch)
    t = ls.tracks["0c:8b:7d:00:00:98"]
    assert t["located"] is False, "one AP cannot yield a position"
    assert "x" not in t


@pytest.mark.skipif(not (DATA / "capture_layer2.jsonl").exists(),
                    reason="run scripts/capture_observations.py first")
def test_replay_capture_locates_cart_panels():
    ls = LiveServer(source=DATA / "capture_layer2.jsonl")
    lines = (DATA / "capture_layer2.jsonl").read_text().splitlines()
    for line in lines[:3]:
        ls._ingest_batch(json.loads(line))
    located = [t for t in ls.tracks.values() if t.get("located")]
    assert located, "no track located from a real capture — pipeline broken?"
    # cart panels use the 0c:8b:7d OUI; at least one should be positioned
    assert any(t["mac"].startswith("0c:8b:7d") for t in located)
    # and the capture must carry no world identity
    assert "cart_" not in (DATA / "capture_layer2.jsonl").read_text()
