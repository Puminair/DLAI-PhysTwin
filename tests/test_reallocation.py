"""Tests for scripts/propose_reallocation.py — FAST by design.

The full proposal run (two coverage_grid passes, ~30 s) is NOT executed
here; it is covered by the @pytest.mark.slow test, skipped by default
(set RUN_SLOW=1 to enable). The fast tests exercise the candidate
filter and donor selection directly, and validate the schema of
data/reallocation_proposal.json if a real run has produced it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_config              # noqa: E402
from sensing.sensor import load_sensors     # noqa: E402
from world.geometry import StoreGeometry    # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "propose_reallocation", ROOT / "scripts" / "propose_reallocation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


realloc = _load_script()


@pytest.fixture(scope="module")
def geo() -> StoreGeometry:
    return StoreGeometry(ROOT / "data" / "store_layer1.geojson")


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def units():
    return load_sensors(ROOT / "data" / "sensing_layer2.json")


@pytest.fixture(scope="module")
def pre_reallocation_layout(units):
    """Donor-selection tests describe the v0.2 layout. Once the proposal
    has been EXECUTED (scripts/apply_reallocation.py), the donors are
    gone from the live layout by design — skip rather than fail."""
    import json
    with open(ROOT / "data" / "sensing_layer2.json", encoding="utf-8") as fh:
        version = json.load(fh).get("provenance", {}).get("dataset_version", "")
    if "reallocated" in version:
        pytest.skip("reallocation already applied to the live layout")
    return units


# -- candidate filter ------------------------------------------------------

def _centroid(fx) -> tuple[float, float]:
    ring = fx.ring[:-1] if fx.ring[0] == fx.ring[-1] else fx.ring
    return (sum(p[0] for p in ring) / len(ring),
            sum(p[1] for p in ring) / len(ring))


@pytest.mark.parametrize("material", ["cold_room", "racking"])
def test_candidate_inside_solid_fixture_rejected(geo, cfg, material):
    mount_z = cfg.get("sensors.mount_z_m")
    fixtures = [f for f in geo.fixtures if f.material == material]
    assert fixtures, f"dataset should contain {material} fixtures"
    for fx in fixtures:
        x, y = _centroid(fx)
        assert not realloc.is_valid_candidate(geo, x, y, mount_z), (
            f"candidate inside {material} at ({x:.1f},{y:.1f}) must be rejected")


def test_candidate_outside_envelope_rejected(geo, cfg):
    mount_z = cfg.get("sensors.mount_z_m")
    assert not realloc.is_valid_candidate(geo, -5.0, -5.0, mount_z)


def test_open_boh_point_accepted(geo, cfg):
    """At least one open BOH grid point must survive the filter."""
    mount_z = cfg.get("sensors.mount_z_m")
    x_split = cfg.get("areas.x_split_m")
    found = False
    y = 1.0
    while y < 55.0 and not found:
        x = x_split + 1.0
        while x < 100.0:
            if (geo.inside_envelope(x, y)
                    and not geo.fixtures_at(x, y, mount_z)):
                assert realloc.is_valid_candidate(geo, x, y, mount_z)
                found = True
                break
            x += 2.0
        y += 2.0
    assert found, "no open BOH mount point found at all — dataset broken?"


# -- donor selection -------------------------------------------------------

def test_donor_selection_returns_k_perimeter_units(pre_reallocation_layout, cfg):
    units = pre_reallocation_layout
    x_split = cfg.get("areas.x_split_m")
    k = 8
    donors = realloc.select_donors(units, k, x_split)
    assert len(donors) == k
    assert len({u.sensor_id for u in donors}) == k, "donors must be unique"
    assert all(u.zone == "perimeter" for u in donors)
    # never donate a unit east of the split — those serve the sparse BOH field
    assert all(u.x < x_split for u in donors)


def test_donor_selection_prefers_west_south_walls(pre_reallocation_layout, cfg):
    units = pre_reallocation_layout
    x_split = cfg.get("areas.x_split_m")
    donors = realloc.select_donors(units, 6, x_split)
    # the 6 west/south sales-side units exist in this dataset; with k=6
    # every donor must come from those walls (x < 5 or y < 5)
    assert all(u.x < 5.0 or u.y < 5.0 for u in donors)


def test_donor_selection_k_too_large_raises(units, cfg):
    with pytest.raises(ValueError):
        realloc.select_donors(units, 99, cfg.get("areas.x_split_m"))


# -- proposal artifact (only when a real run has produced it) --------------

PROPOSAL = ROOT / "data" / "reallocation_proposal.json"


@pytest.mark.skipif(not PROPOSAL.exists(),
                    reason="run scripts/propose_reallocation.py first")
def test_proposal_schema_and_improvement():
    doc = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    for key in ("provenance", "k", "donors", "new_positions",
                "before", "after"):
        assert key in doc, f"missing key: {key}"
    assert len(doc["donors"]) == doc["k"]
    assert len(doc["new_positions"]) <= doc["k"]
    for p in doc["new_positions"]:
        assert p["zone"] == "back_of_house"
        assert {"x", "y", "z"} <= p.keys()
    for zone in ("back_of_house", "sales_floor"):
        for phase in ("before", "after"):
            s = doc[phase][zone]
            assert {"cells", "ge3ap_pct", "blind_pct"} <= s.keys()
    # the point of it all: reallocation must not make BOH worse
    assert (doc["after"]["back_of_house"]["ge3ap_pct"]
            >= doc["before"]["back_of_house"]["ge3ap_pct"])


# -- full run (slow — two coverage_grid passes, ~30 s) ---------------------

@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("RUN_SLOW"),
                    reason="slow full run; set RUN_SLOW=1 to enable")
def test_full_reallocation_run(tmp_path):
    out = tmp_path / "proposal.json"
    proposal = realloc.main(["--out", str(out)])
    assert out.exists()
    assert (proposal["after"]["back_of_house"]["ge3ap_pct"]
            >= proposal["before"]["back_of_house"]["ge3ap_pct"])
