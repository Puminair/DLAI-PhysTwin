"""Layer 1 — the staff roster: roles, posts, and shift behaviour.

May import: stdlib, config, world.*.

A full shift complement (counts are `assumed` config, no roster document
exists): cashiers hold their lanes, stockers restock gondola bays,
warehouse workers loop racking corridors and docks, prep staff work
their rooms, security holds the gates, managers alternate office and
floor walks. Lanes without a cashier are closed — the cart queueing in
sim.py sees that directly.

Staff are also bodies: Layer 2 counts them as RF absorbers exactly like
shoppers, which is part of why a full shift changes the flicker picture.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from world.entities import Staff
from world.geometry import StoreGeometry
from world.motion import AisleGraph, step_towards


@dataclass
class StaffRoster:
    geometry: StoreGeometry
    sales_graph: AisleGraph
    cfg: "object"
    rng: random.Random
    checkouts: dict                       # sim's lane map (fid -> Checkout)
    members: list[Staff] = field(default_factory=list)

    def __post_init__(self):
        st = self.cfg.section("staffing")
        self._speed = tuple(st["speed_ms"])
        self._restock = tuple(st["restock_dwell_s"])
        self._wh_dwell = tuple(st["warehouse_dwell_s"])
        self._walk_dwell = tuple(st["floorwalk_dwell_s"])
        self._gondolas = [f for f in self.geometry.fixtures if f.kind == "gondola"]
        self._rooms = {f.fid: f for f in self.geometry.fixtures if f.kind == "room"}
        self._gates = [f for f in self.geometry.fixtures if f.kind == "gate"]
        self._docks = [f for f in self.geometry.fixtures if f.kind == "dock"]
        self.warehouse_graph = self._build_warehouse_graph()
        self._spawn(st)

    # -- construction --------------------------------------------------
    def _build_warehouse_graph(self) -> AisleGraph:
        """Corridors between racking rows, spine west of the racks."""
        rack_ys = sorted({round(sum(p[1] for p in f.ring[:-1]) / 4, 1)
                          for f in self.geometry.fixtures if f.kind == "racking"})
        if len(rack_ys) >= 2:
            corridor_ys = tuple((a + b) / 2 for a, b in zip(rack_ys, rack_ys[1:]))
        else:
            corridor_ys = (25.0,)
        return AisleGraph(aisle_ys=corridor_ys, x_min=74.0, x_max=96.5,
                          front_runway_x=72.5, back_runway_x=97.0)

    @staticmethod
    def _centre(fx) -> tuple[float, float]:
        return (sum(p[0] for p in fx.ring[:-1]) / 4,
                sum(p[1] for p in fx.ring[:-1]) / 4)

    def _add(self, role: str, x: float, y: float, note: str) -> Staff:
        m = Staff(id=f"staff_{len(self.members):03d}",
                  provenance=f"roster: {note}", role=role,
                  x=x, y=y, post_x=x, post_y=y,
                  speed_ms=self.rng.uniform(*self._speed))
        self.members.append(m)
        return m

    def _spawn(self, st: dict):
        # cashiers: one per lane in fid order; the rest of the lanes close
        lanes = sorted(self.checkouts.values(), key=lambda c: c.id)
        for i, lane in enumerate(lanes):
            if i < st["cashiers"]:
                self._add("cashier", lane.x - 0.9, lane.y, f"lane {lane.id}")
                lane.open = True
            else:
                lane.open = False

        for i in range(st["stockers"]):
            g = self.rng.choice(self._gondolas)
            gx, gy = self._centre(g)
            self._add("stocker", gx, self.sales_graph.nearest_aisle_y(gy),
                      "restock rotation")

        for i in range(st["warehouse_workers"]):
            y = self.rng.choice(self.warehouse_graph.aisle_ys)
            self._add("warehouse", 74.0 + i * 4.0, y, "racking/dock loop")

        prep_rooms = [fid for fid in ("prep_meat", "prep_bakery", "prep_deli",
                                      "staff_room") if fid in self._rooms]
        for i in range(st["prep_staff"]):
            room = self._rooms[prep_rooms[i % len(prep_rooms)]]
            cx, cy = self._centre(room)
            m = self._add("prep", cx, cy, f"room {room.fid}")
            m.provenance += f"|room:{room.fid}"

        for i in range(st["security"]):
            gate = self._gates[i % len(self._gates)]
            gx, gy = self._centre(gate)
            self._add("security", gx + 1.0, gy + 1.2, f"post {gate.fid}")

        office = self._rooms.get("office")
        ox, oy = self._centre(office) if office else (62.0, 12.0)
        for i in range(st["managers"]):
            self._add("manager", ox + i, oy, "office + floor walks")

    # -- behaviour -----------------------------------------------------
    def step(self, dt_s: float, now_s: float):
        for m in self.members:
            if m.role == "cashier":
                self._jitter_at_post(m, dt_s, radius=0.4)
            elif m.role == "security":
                self._jitter_at_post(m, dt_s, radius=0.8)
            elif m.role == "prep":
                self._jitter_at_post(m, dt_s, radius=1.5)
            elif m.role == "stocker":
                self._patrol(m, dt_s, now_s, self.sales_graph.route,
                             self._next_gondola_stop, self._restock)
            elif m.role == "warehouse":
                self._patrol(m, dt_s, now_s, self.warehouse_graph.route,
                             self._next_warehouse_stop, self._wh_dwell)
            elif m.role == "manager":
                self._patrol(m, dt_s, now_s, self._route_across_split,
                             self._next_manager_stop, self._walk_dwell)

    def _jitter_at_post(self, m: Staff, dt_s: float, radius: float):
        tx = m.post_x + self.rng.uniform(-radius, radius)
        ty = m.post_y + self.rng.uniform(-radius, radius)
        m.x, m.y, _ = step_towards(m.x, m.y, tx, ty, 0.3 * dt_s)

    def _patrol(self, m: Staff, dt_s: float, now_s: float, route_fn,
                next_stop, dwell_range: tuple[float, float]):
        if now_s < m.dwell_until_s:
            return
        if not m.waypoints:
            m.waypoints = route_fn((m.x, m.y), next_stop(m))
            m.dwell_until_s = now_s + self.rng.uniform(*dwell_range)
            return
        tx, ty = m.waypoints[0]
        m.x, m.y, arrived = step_towards(m.x, m.y, tx, ty, m.speed_ms * dt_s)
        if arrived:
            m.waypoints.pop(0)

    def _route_across_split(self, start: tuple[float, float],
                            goal: tuple[float, float]) -> list[tuple[float, float]]:
        """Route that respects the split wall: cross only at the doorway.

        The wall has doorways at y=19 and y=37; whichever side the leg
        starts on, a crossing leg is stitched through the nearer door.
        """
        split = self.cfg.get("areas.x_split_m")
        if (start[0] - split) * (goal[0] - split) >= 0:
            if start[0] >= split and goal[0] >= split:
                return [start, (start[0], goal[1]), goal]   # BOH free walk
            return self.sales_graph.route(start, goal)
        door_y = 19.0 if abs(start[1] - 19.0) <= abs(start[1] - 37.0) else 37.0
        east, west = (split + 1.2, door_y), (split - 1.2, door_y)
        if start[0] >= split:   # BOH -> sales floor
            return [(start[0], door_y), east, west] + self.sales_graph.route(west, goal)
        return self.sales_graph.route(start, west)[:-1] + [west, east, (goal[0], door_y), goal]

    def _next_gondola_stop(self, m: Staff) -> tuple[float, float]:
        g = self.rng.choice(self._gondolas)
        gx, gy = self._centre(g)
        return (gx, self.sales_graph.nearest_aisle_y(gy))

    def _next_warehouse_stop(self, m: Staff) -> tuple[float, float]:
        if self._docks and self.rng.random() < 0.3:
            dx, dy = self._centre(self.rng.choice(self._docks))
            return (dx - 2.0, dy)     # stand off the dock face
        y = self.rng.choice(self.warehouse_graph.aisle_ys)
        return (self.rng.uniform(75.0, 96.0), y)

    def _next_manager_stop(self, m: Staff) -> tuple[float, float]:
        if self.rng.random() < 0.4:
            return (m.post_x, m.post_y)   # back to the office
        g = self.rng.choice(self._gondolas)
        gx, gy = self._centre(g)
        return (gx, self.sales_graph.nearest_aisle_y(gy))
