"""Layer 1 — the world simulation orchestrator.

May import: stdlib, config, world.*.

Runs the store: shoppers arrive, take a cart, visit gondolas, queue,
pay, exit, and the cart returns to the dock. Produces a coherent event
trace with sensing disabled — that is the Layer-1 acceptance criterion.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from world.clock import Clock
from world.entities import Cart, CartState, Checkout, Gate, Shopper
from world.events import EventLog
from world.geometry import StoreGeometry
from world.motion import AisleGraph, advance_cart, advance_shopper


@dataclass
class Trip:
    cart_id: str
    shopper_id: str
    stops_left: int
    checkout_id: str | None = None


@dataclass
class WorldSim:
    geometry: StoreGeometry
    cfg: "object"
    clock: Clock = field(default_factory=Clock)
    seed: int = 7
    n_carts: int = 30
    arrival_rate_per_min: float = 1.5

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.events = EventLog(self.clock)
        self.graph = AisleGraph.from_config(self.cfg)
        mo = self.cfg.section("motion")
        self._cart_speed = tuple(mo["cart_speed_ms"])
        self._shopper_speed = tuple(mo["shopper_speed_ms"])
        self._dwell = tuple(mo["gondola_dwell_s"])
        self._service = tuple(mo["checkout_service_s"])

        dock = next(f for f in self.geometry.fixtures if f.kind == "cart_dock")
        dx = sum(p[0] for p in dock.ring[:-1]) / 4
        dy = sum(p[1] for p in dock.ring[:-1]) / 4
        self.dock_xy = (dx, dy)
        self.carts = {f"cart_{i:03d}": Cart(id=f"cart_{i:03d}",
                                            provenance="sim: fleet at dock",
                                            x=dx, y=dy)
                      for i in range(self.n_carts)}
        self.checkouts = {}
        for f in self.geometry.fixtures:
            if f.kind == "checkout":
                cx = sum(p[0] for p in f.ring[:-1]) / 4
                cy = sum(p[1] for p in f.ring[:-1]) / 4
                self.checkouts[f.fid] = Checkout(id=f.fid, provenance="geojson",
                                                 x=cx + 1.8, y=cy)
        self.gates = [Gate(id=f.fid, provenance="geojson",
                           x=sum(p[0] for p in f.ring[:-1]) / 4,
                           y=sum(p[1] for p in f.ring[:-1]) / 4,
                           direction="entry" if "entry" in f.fid else "exit")
                      for f in self.geometry.fixtures if f.kind == "gate"]
        self._gondolas = [f for f in self.geometry.fixtures if f.kind == "gondola"]
        self.shoppers: dict[str, Shopper] = {}
        self.trips: dict[str, Trip] = {}          # keyed by cart_id
        self._n_shoppers = 0
        self._arrival_debt = 0.0
        from world.staffing import StaffRoster
        self.staff = StaffRoster(geometry=self.geometry, sales_graph=self.graph,
                                 cfg=self.cfg, rng=random.Random(self.seed + 1),
                                 checkouts=self.checkouts)

    # -- helpers -------------------------------------------------------
    def _free_cart(self) -> Cart | None:
        return next((c for c in self.carts.values()
                     if c.state is CartState.DOCKED), None)

    def _gondola_stop(self) -> tuple[float, float]:
        """A point in the aisle adjacent to a random gondola bay."""
        g = self.rng.choice(self._gondolas)
        cx = sum(p[0] for p in g.ring[:-1]) / 4
        cy = sum(p[1] for p in g.ring[:-1]) / 4
        return (cx, self.graph.nearest_aisle_y(cy))

    def _start_trip(self):
        cart = self._free_cart()
        if cart is None:
            return
        self._n_shoppers += 1
        sid = f"shopper_{self._n_shoppers:04d}"
        shopper = Shopper(id=sid, provenance="sim: arrival process",
                          x=cart.x, y=cart.y, cart_id=cart.id,
                          speed_ms=self.rng.uniform(*self._shopper_speed))
        self.shoppers[sid] = shopper
        cart.state = CartState.SHOPPING
        cart.speed_ms = self.rng.uniform(*self._cart_speed)
        stops = self.rng.randint(3, 9)
        self.trips[cart.id] = Trip(cart_id=cart.id, shopper_id=sid, stops_left=stops)
        first = self._gondola_stop()
        cart.waypoints = self.graph.route((cart.x, cart.y), first)
        self.events.emit("cart_undocked", cart.id, cart.x, cart.y, shopper=sid)

    def _next_leg(self, cart: Cart, trip: Trip):
        now = self.clock.elapsed_s()
        if trip.stops_left > 0:
            trip.stops_left -= 1
            cart.dwell_until_s = now + self.rng.uniform(*self._dwell)
            cart.items += 1
            self.events.emit("item_picked", cart.id, cart.x, cart.y,
                             shopper=trip.shopper_id)
            nxt = self._gondola_stop()
            cart.waypoints = self.graph.route((cart.x, cart.y), nxt)
        elif trip.checkout_id is None:
            co = min((c for c in self.checkouts.values() if c.open),
                     key=lambda c: len(c.queue))
            trip.checkout_id = co.id
            co.queue.append(cart.id)
            cart.state = CartState.QUEUING
            cart.waypoints = self.graph.route((cart.x, cart.y), (co.x, co.y))

    def step(self, dt_s: float):
        self.clock.advance(dt_s)
        now = self.clock.elapsed_s()

        # arrivals (deterministic thinning of a Poisson-ish process)
        self._arrival_debt += self.arrival_rate_per_min * dt_s / 60.0
        while self._arrival_debt >= 1.0:
            self._arrival_debt -= 1.0
            self._start_trip()

        for cart in self.carts.values():
            trip = self.trips.get(cart.id)
            if cart.state is CartState.SHOPPING:
                if now < cart.dwell_until_s:
                    continue
                advance_cart(cart, dt_s)
                if not cart.waypoints:
                    self._next_leg(cart, trip)
            elif cart.state is CartState.QUEUING:
                co = self.checkouts[trip.checkout_id]
                pos = co.queue.index(cart.id)
                # stand-off point: 1.2 m per queue slot behind the lane head
                tx, ty = co.x + 1.2 * pos, co.y
                if cart.waypoints:
                    advance_cart(cart, dt_s)
                else:
                    from world.motion import step_towards
                    cart.x, cart.y, _ = step_towards(cart.x, cart.y, tx, ty,
                                                     cart.speed_ms * dt_s)
                if pos == 0 and abs(cart.x - co.x) < 0.3 and now >= co.busy_until_s:
                    cart.state = CartState.PAYING
                    co.busy_until_s = now + self.rng.uniform(*self._service)
                    self.events.emit("payment_started", cart.id, cart.x, cart.y,
                                     checkout=co.id)
            elif cart.state is CartState.PAYING:
                co = self.checkouts[trip.checkout_id]
                if now >= co.busy_until_s:
                    co.queue.remove(cart.id)
                    self.events.emit("payment_completed", cart.id, cart.x, cart.y,
                                     checkout=co.id, items=cart.items)
                    gate = self.rng.choice([g for g in self.gates
                                            if g.direction == "exit"])
                    cart.state = CartState.EXITING
                    cart.waypoints = [(cart.x, 3.0), (gate.x, gate.y),
                                      self.dock_xy]
                    self.events.emit("cart_exited_gate", cart.id, gate.x, gate.y,
                                     gate=gate.id)
            elif cart.state is CartState.EXITING:
                advance_cart(cart, dt_s)
                if not cart.waypoints:
                    cart.state = CartState.DOCKED
                    cart.items = 0
                    self.events.emit("cart_docked", cart.id, cart.x, cart.y)
                    sid = trip.shopper_id
                    self.shoppers.pop(sid, None)
                    self.trips.pop(cart.id, None)

        for shopper in self.shoppers.values():
            cart = self.carts.get(shopper.cart_id) if shopper.cart_id else None
            advance_shopper(shopper, cart, self.rng, dt_s)

        self.staff.step(dt_s, now)

    def run(self, hours: float, dt_s: float = 0.5):
        steps = int(hours * 3600 / dt_s)
        for _ in range(steps):
            self.step(dt_s)

    # -- ground-truth snapshot (facts, no sensors) ---------------------
    def snapshot(self) -> dict:
        return {
            "t_ms": self.clock.now_ms(),
            "carts": [{"id": c.id, "x": round(c.x, 3), "y": round(c.y, 3),
                       "state": c.state.value}
                      for c in self.carts.values()],
            "shoppers": [{"id": s.id, "x": round(s.x, 3), "y": round(s.y, 3)}
                         for s in self.shoppers.values()],
            "staff": [{"id": m.id, "x": round(m.x, 3), "y": round(m.y, 3),
                       "role": m.role}
                      for m in self.staff.members],
        }
