"""Layer 3: ingest normalisation, elimination, attack assessment."""
from __future__ import annotations

from pathlib import Path

import pytest

from dlai.attack import AttackAnalyzer
from dlai.entity import EntityResolver, rssi_to_distance_m
from dlai.floorplan import FloorPlan
from dlai.ingest import load_sensor_sites, normalise_batch
from sensing.aireye import AirEyeEmulator

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def plan():
    return FloorPlan(DATA / "store_layer1.geojson")


@pytest.fixture(scope="module")
def sites():
    return load_sensor_sites(DATA / "sensing_layer2.json")


def test_ingest_normalises_positive_rssi_and_records_it():
    batch = {"deliveredAt": 2000, "records": [{
        "stream": "scanning_api_v3/DevicesSeen", "clientMac": "aa:aa:aa:00:00:01",
        "seenTime": 1000, "locations": [],
        "rssiRecords": [{"apMac": "m1", "rssi": 58.0}],
        "gap": ["no_location_lt_min_aps"], "manufacturer": "X"}]}
    out = normalise_batch(batch)
    assert out[0].rssi_dbm == [("m1", -58.0)]
    assert any("rssi_sign_flipped" in n for n in out[0].normalisation), (
        "normalisation must be recorded, not silent")
    assert "no_location_lt_min_aps" in out[0].gaps


def test_elimination_rules(plan):
    # outside the envelope: impossible
    assert not plan.possible(-5.0, 10.0, None, 0.0)
    # inside a gondola at panel height: impossible
    g = next(f for f in plan.fixtures if f.kind == "gondola")
    gx = sum(p[0] for p in g.ring[:-1]) / 4
    gy = sum(p[1] for p in g.ring[:-1]) / 4
    assert not plan.possible(gx, gy, None, 0.0)
    # unreachable at walking speed: impossible
    assert not plan.possible(40.0, 25.0, (10.0, 25.0), 2.0)
    # an ordinary aisle point: possible
    assert plan.possible(20.0, 5.4, (19.0, 5.4), 2.0)


def test_rssi_ranging_is_monotonic():
    assert rssi_to_distance_m(-40) < rssi_to_distance_m(-55) < rssi_to_distance_m(-67)


def test_resolver_carries_variance_and_never_claims_observation(sites, plan):
    from dlai.ingest import NormalisedObservation
    resolver = EntityResolver(sites, plan)
    macs = list(sites)[:4]
    obs = NormalisedObservation(
        mac="aa:aa:aa:00:00:02", seen_time_ms=1000, delivered_at_ms=2000,
        rssi_dbm=[(m, -50.0 - i * 3) for i, m in enumerate(macs)],
        cloud_location=None, gaps=[])
    est = resolver.consume(obs)
    assert est is not None
    assert est.confidence == "inferred", "a position is never an observation"
    assert est.variance_m2 > 0, "variance must never be collapsed"
    assert est.candidates, "the distribution survives to the output"


def test_below_min_aps_yields_no_position_and_flicker_is_counted(sites, plan):
    from dlai.ingest import NormalisedObservation
    resolver = EntityResolver(sites, plan)
    macs = list(sites)[:4]

    def ob(t, n):
        return NormalisedObservation(
            mac="aa:aa:aa:00:00:03", seen_time_ms=t, delivered_at_ms=t,
            rssi_dbm=[(m, -50.0) for m in macs[:n]], cloud_location=None,
            gaps=[])
    assert resolver.consume(ob(1000, 2)) is None
    resolver.consume(ob(2000, 4))
    resolver.consume(ob(3000, 2))
    resolver.consume(ob(4000, 4))
    track = resolver.tracks["aa:aa:aa:00:00:03"]
    assert track.flicker_transitions == 3, "oscillation around the 3-AP rule"


def test_attack_rogue_on_wire_vs_neighbour():
    em = AirEyeEmulator()
    an = AttackAnalyzer()
    for r in em.neighbour_records(t_ms=10_000):
        a = an.consume(r)
        assert a.kind == "neighbour_network" and a.severity == "info", (
            "wiredMacs is the field separating rogue from neighbour")
    rogue = an.consume(em.rogue_on_wire_record(t_ms=10_000))
    assert rogue.kind == "rogue_ap_on_wire" and rogue.severity == "high"
    assert rogue.mode == "RECOMMEND_ONLY", "no automated blocking in the PoC"
    assert "recommend" in rogue.action


def test_awips_throttling_loses_intensity():
    em = AirEyeEmulator()
    an = AttackAnalyzer()
    emitted = [em.awips_event("deauth_flood", "ap1", t) for t in
               range(0, 30_000, 1000)]
    delivered = [e for e in emitted if e is not None]
    assert len(delivered) == 1, "one message per signature per AP per interval"
    a = an.consume(delivered[0])
    assert "no_client_identity" in delivered[0]["gap"]
    assert "intensity" in a.action.lower()
