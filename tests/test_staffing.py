"""Layer 1 staff roster + its Layer-2 consequences (bodies, phones)."""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pytest

from config import load_config
from sensing.pipeline import ObservationPipeline
from sensing.sensor import SensorField, load_sensors
from world.geometry import StoreGeometry
from world.sim import WorldSim

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def sim_run(cfg):
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    sim = WorldSim(geometry=geo, cfg=cfg, seed=21)
    sim.run(hours=0.25, dt_s=0.5)
    return sim


def test_roster_counts_come_from_config(cfg, sim_run):
    st = cfg.section("staffing")
    expected = {"cashier": st["cashiers"], "stocker": st["stockers"],
                "warehouse": st["warehouse_workers"], "prep": st["prep_staff"],
                "security": st["security"], "manager": st["managers"]}
    assert Counter(m.role for m in sim_run.staff.members) == Counter(expected)


def test_cashiers_hold_their_lanes_and_extra_lanes_close(cfg, sim_run):
    cashiers = [m for m in sim_run.staff.members if m.role == "cashier"]
    for m in cashiers:
        assert math.dist((m.x, m.y), (m.post_x, m.post_y)) < 1.5
    n_open = sum(1 for c in sim_run.checkouts.values() if c.open)
    assert n_open == cfg.get("staffing.cashiers")


def test_staff_stay_inside_the_envelope(sim_run):
    geo = sim_run.geometry
    for m in sim_run.staff.members:
        assert geo.inside_envelope(m.x, m.y), f"{m.id} ({m.role}) escaped"


def test_warehouse_workers_stay_in_back_of_house(cfg, sim_run):
    split = cfg.get("areas.x_split_m")
    for m in sim_run.staff.members:
        if m.role in ("warehouse", "prep"):
            assert m.x > split, f"{m.id} ({m.role}) crossed to the sales floor"


def test_staff_are_bodies_for_rf(cfg, sim_run):
    snap = sim_run.snapshot()
    bodies = ObservationPipeline.bodies_xy(snap)
    assert len(bodies) == len(snap["shoppers"]) + len(snap["staff"])


def test_staff_phones_are_observed_with_stable_macs(cfg):
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                       geometry=geo, cfg=cfg)
    sim = WorldSim(geometry=geo, cfg=cfg, seed=22)
    pipe = ObservationPipeline(field_=field, cfg=cfg, seed=4)
    for i in range(240):     # 2 sim minutes
        sim.step(0.5)
        if i % 20 == 19:
            pipe.observe(sim.snapshot())
    pipe.flush_all(sim.clock.now_ms())
    staff_macs = {rf.mac for wid, rf in pipe._rf.items()
                  if wid.startswith("staff_")}
    assert staff_macs, "staff phones were never registered"
    emitted = {r["clientMac"] for b in pipe.delivered for r in b.records}
    assert staff_macs & emitted, "no staff phone reached the API"
    # associated => the record carries the ops SSID, and no world id leaks
    for b in pipe.delivered:
        for r in b.records:
            if r["clientMac"] in staff_macs:
                assert r["ssid"] == "centro-ops"
                assert "staff_" not in str(r)
