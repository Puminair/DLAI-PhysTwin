"""Layer 2: propagation physics, observation shapes, modelled gaps."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from config import load_config
from sensing.observation import (bluetooth_devices_seen_record,
                                 devices_seen_record)
from sensing.pipeline import ObservationPipeline
from sensing.propagation import (bodies_crossed, crossing_loss_db, fspl_db,
                                 uplink_rssi_dbm)
from sensing.sensor import SensorField, load_sensors
from world.geometry import StoreGeometry
from world.sim import WorldSim

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def field(cfg):
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    return SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                       geometry=geo, cfg=cfg)


def test_fspl_reference_value():
    # 10 m at 2437 MHz: 20log10(10) + 20log10(2437) - 27.55 = 60.18 dB
    assert fspl_db(10.0, "2.4") == pytest.approx(60.18, abs=0.05)


def test_uplink_asymmetry_governs_the_budget(cfg):
    """The 8-14 dB EIRP asymmetry: modelling the downlink is wrong in the
    optimistic direction. Same path, ap-EIRP vs cart-EIRP."""
    att = {b: dict(v) for b, v in cfg.section("attenuation_db").items()
           if b != "human_body"}
    body = cfg.get("attenuation_db.human_body")
    kwargs = dict(band="2.4", sensor_antenna_gain_dbi=4.0,
                  diversity_gain_db=2.5, distance_m=15.0,
                  crossings=Counter({"gondola": 1}), n_bodies=0,
                  attenuation_db=att, body_loss_db=body)
    up = uplink_rssi_dbm(device_eirp_dbm=17.0, **kwargs)
    down_style = uplink_rssi_dbm(device_eirp_dbm=24.0, **kwargs)
    assert down_style - up == pytest.approx(7.0)
    assert up < down_style, "downlink flatters the budget"


def test_opaque_material_kills_the_link(cfg):
    att = {b: dict(v) for b, v in cfg.section("attenuation_db").items()
           if b != "human_body"}
    assert crossing_loss_db(Counter({"cold_room": 1}), "2.4", att) is None
    r = uplink_rssi_dbm(band="2.4", device_eirp_dbm=17.0,
                        sensor_antenna_gain_dbi=4.0, diversity_gain_db=2.5,
                        distance_m=5.0, crossings=Counter({"cold_room": 1}),
                        n_bodies=0, attenuation_db=att,
                        body_loss_db=cfg.get("attenuation_db.human_body"))
    assert r is None, "metal is a conductor, not a lossy dielectric"


def test_bodies_are_dynamic_absorbers():
    a, b = (0.0, 0.0, 3.0), (10.0, 0.0, 1.0)
    on_path = [(5.0, 0.1)]        # within shoulder half-width, ray z=2.0<=1.8? no
    # at t=0.5 ray z is 2.0 — above a 1.8 m body: must NOT count
    assert bodies_crossed(a, b, on_path, 0.28) == 0
    near_device = [(8.0, 0.1)]    # t=0.8 -> z=1.4, below 1.8: counts
    assert bodies_crossed(a, b, near_device, 0.28) == 1
    off_path = [(5.0, 3.0)]
    assert bodies_crossed(a, b, off_path, 0.28) == 0


def test_band_ordering_2g4_hears_more_than_5_and_6(field, cfg):
    """2.4 GHz wins for positioning, decisively: a gondola costs 17.7 dB
    at 2.4 and 36.7 at 6 — the 2 dB of extra antenna gain does not pay."""
    xyz = (25.0, 27.0, cfg.get("heights.cart_panel_z_m"))
    n = {}
    for band in ("2.4", "5", "6"):
        eirp = cfg.get("cart_uplink.eirp_dbm")[band]
        n[band] = len(field.hear_device(band=band, eirp_dbm=eirp,
                                        device_xyz=xyz, shopper_xy=[],
                                        use_reflections_on_fail=False))
    assert n["2.4"] > n["5"] >= n["6"]


def test_observation_shapes_and_sign_conventions():
    ds = devices_seen_record(client_mac="aa:bb:cc:00:00:01", ipv4=None,
                             ssid="centro-ops", os_hint=None,
                             manufacturer="X", seen_time_ms=1,
                             floor_plan_id="f", location=None,
                             rssi_records=[{"apMac": "m", "rssi": -55.0}],
                             gaps=[])
    assert ds["rssiRecords"][0]["rssi"] == 55.0, "DevicesSeen is POSITIVE"
    bt = bluetooth_devices_seen_record(client_mac="aa:bb:cc:00:00:02",
                                       manufacturer="X", seen_time_ms=1,
                                       rssi_records=[{"apMac": "m", "rssi": -60.0}],
                                       gaps=[])
    assert bt["rssiRecords"][0]["rssi"] == -60.0, "Bluetooth is NEGATIVE"
    for key in ("clientMac", "ssid", "os", "manufacturer", "locations",
                "rssiRecords", "confidence", "gap"):
        assert key in ds


def test_pipeline_gaps_randomized_macs_never_reach_the_api(cfg, field):
    geo = field.geometry
    sim = WorldSim(geometry=geo, cfg=cfg, seed=11)
    pipe = ObservationPipeline(field_=field, cfg=cfg, seed=1)
    for i in range(600):     # 5 sim minutes
        sim.step(0.5)
        if i % 20 == 19:
            pipe.observe(sim.snapshot())
    pipe.flush_all(sim.clock.now_ms())
    emitted_macs = {r["clientMac"] for b in pipe.delivered for r in b.records}
    randomized = {rf.mac for rf in pipe._rf.values()
                  if rf.randomized and not rf.associated}
    assert randomized, "sim should have produced randomized phones"
    assert not randomized & emitted_macs, (
        "unassociated randomized MACs must be dropped before the API")


def test_pipeline_batches_are_jittered_not_instant(cfg, field):
    lo, hi = cfg.get("pipeline.post_interval_s")
    geo = field.geometry
    sim = WorldSim(geometry=geo, cfg=cfg, seed=12)
    pipe = ObservationPipeline(field_=field, cfg=cfg, seed=2)
    for i in range(int(12 * 60 / 0.5)):
        sim.step(0.5)
        if i % 10 == 9:
            pipe.observe(sim.snapshot())
    assert len(pipe.delivered) >= 2
    gaps_s = [(b2.delivered_at_ms - b1.delivered_at_ms) / 1000.0
              for b1, b2 in zip(pipe.delivered, pipe.delivered[1:])]
    for g in gaps_s:
        assert lo - 6.0 <= g <= hi + 6.0, f"POST gap {g}s outside jitter window"
    # latency: records are delivered after they were seen
    for b in pipe.delivered:
        for r in b.records:
            assert r["seenTime"] <= b.delivered_at_ms


def test_no_record_ever_links_cart_id_and_mac(cfg, field):
    """The identity trap, enforced at the output boundary."""
    sim = WorldSim(geometry=field.geometry, cfg=cfg, seed=13)
    pipe = ObservationPipeline(field_=field, cfg=cfg, seed=3)
    for i in range(400):
        sim.step(0.5)
        if i % 20 == 19:
            pipe.observe(sim.snapshot())
    pipe.flush_all(sim.clock.now_ms())
    import json
    for b in pipe.delivered:
        blob = json.dumps(b.to_json())
        assert "cart_" not in blob, "a world id leaked into an observation"
        assert "shopper_" not in blob, "a world id leaked into an observation"
