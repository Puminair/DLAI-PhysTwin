"""Layer 1 acceptance: the world runs with sensing disabled and produces
a coherent trace. If it cannot, Layer 1 is not independent."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pytest

from config import load_config
from world.clock import Clock
from world.geometry import StoreGeometry
from world.sim import WorldSim

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def sim_hour():
    cfg = load_config()
    geo = StoreGeometry(DATA / "store_layer1.geojson")
    sim = WorldSim(geometry=geo, cfg=cfg, seed=7)
    sim.run(hours=1.0, dt_s=0.5)
    return sim


def test_clock_is_monotonic_and_injectable():
    c = Clock(epoch_ms=1000)
    assert c.now_ms() == 1000
    c.advance(1.5)
    assert c.now_ms() == 2500
    with pytest.raises(ValueError):
        c.advance(-1)


def test_one_simulated_hour_produces_a_coherent_trace(sim_hour):
    from world.entities import CartState
    counts = Counter(e.type for e in sim_hour.events.events)
    assert counts["cart_undocked"] > 0
    assert counts["item_picked"] > 0
    # every started payment completes, except those still in progress at the
    # one-hour cutoff (carts currently in PAYING) — a boundary effect, not a leak
    in_flight = sum(1 for c in sim_hour.carts.values()
                    if c.state is CartState.PAYING)
    assert counts["payment_started"] - counts["payment_completed"] == in_flight
    assert counts["cart_exited_gate"] == counts["payment_completed"]
    # every docked cart completed the full lifecycle in order
    by_cart = defaultdict(list)
    for e in sim_hour.events.events:
        by_cart[e.entity_id].append(e.type)
    for cart_id, seq in by_cart.items():
        for trip_start in (i for i, t in enumerate(seq) if t == "cart_undocked"):
            rest = seq[trip_start:]
            if "cart_docked" in rest:
                dock_i = rest.index("cart_docked")
                trip = rest[:dock_i]
                assert "payment_started" in trip, f"{cart_id} docked without paying"
                assert trip.index("payment_started") < trip.index("cart_exited_gate")


def test_events_carry_true_timestamps_in_order(sim_hour):
    ts = [e.t_ms for e in sim_hour.events.events]
    assert ts == sorted(ts)
    assert all(t >= sim_hour.clock.epoch_ms for t in ts)


def test_all_positions_stay_inside_the_envelope(sim_hour):
    geo = sim_hour.geometry
    for c in sim_hour.carts.values():
        assert geo.inside_envelope(c.x, c.y), f"{c.id} escaped at {c.x},{c.y}"


def test_cart_speeds_within_spec(sim_hour):
    lo, hi = load_config().get("motion.cart_speed_ms")
    for c in sim_hour.carts.values():
        assert lo <= c.speed_ms <= hi
