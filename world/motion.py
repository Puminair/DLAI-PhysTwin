"""Layer 1 — motion: waypoint navigation, dwell, queueing.

May import: stdlib, config, world.*.

Carts follow aisle centrelines with dwell at gondolas; shoppers walk
more freely near their cart. Carts queue at checkout. Routing is
L-shaped over the aisle graph: along the current aisle to a runway,
along the runway, into the target aisle.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from world.entities import Cart, Shopper


@dataclass(frozen=True)
class AisleGraph:
    """Aisle centrelines plus the two runways that connect them."""
    aisle_ys: tuple[float, ...]
    x_min: float          # west end of aisle runs
    x_max: float          # east end of aisle runs
    front_runway_x: float
    back_runway_x: float

    @classmethod
    def from_config(cls, cfg) -> "AisleGraph":
        g = cfg.section("geometry")
        ys = tuple(g["aisle_y_start_m"] + k * g["aisle_pitch_m"]
                   for k in range(g["aisles"]))
        x0 = g["aisle_x_start_m"]
        x1 = x0 + g["aisle_run_m"]
        return cls(aisle_ys=ys, x_min=x0, x_max=x1,
                   front_runway_x=x0 - 2.0, back_runway_x=x1 + 2.0)

    def nearest_aisle_y(self, y: float) -> float:
        return min(self.aisle_ys, key=lambda ay: abs(ay - y))

    def route(self, start: tuple[float, float],
              goal: tuple[float, float]) -> list[tuple[float, float]]:
        """Waypoints from start to goal along aisles and runways."""
        sy, gy = self.nearest_aisle_y(start[1]), self.nearest_aisle_y(goal[1])
        if abs(sy - gy) < 1e-9:
            return [(start[0], sy), (goal[0], sy), goal]
        # choose the runway needing less backtracking
        runway = (self.front_runway_x
                  if abs(start[0] - self.front_runway_x) + abs(goal[0] - self.front_runway_x)
                  <= abs(start[0] - self.back_runway_x) + abs(goal[0] - self.back_runway_x)
                  else self.back_runway_x)
        return [(start[0], sy), (runway, sy), (runway, gy), (goal[0], gy), goal]


def step_towards(x: float, y: float, tx: float, ty: float,
                 dist: float) -> tuple[float, float, bool]:
    """Move up to `dist` metres towards (tx, ty); True when arrived."""
    d = math.hypot(tx - x, ty - y)
    if d <= dist or d < 1e-9:
        return tx, ty, True
    f = dist / d
    return x + (tx - x) * f, y + (ty - y) * f, False


def advance_cart(cart: Cart, dt_s: float) -> bool:
    """Advance a cart along its waypoint list. True if a waypoint was consumed."""
    if not cart.waypoints:
        return False
    tx, ty = cart.waypoints[0]
    cart.x, cart.y, arrived = step_towards(cart.x, cart.y, tx, ty,
                                           cart.speed_ms * dt_s)
    if arrived:
        cart.waypoints.pop(0)
    return arrived


def advance_shopper(shopper: Shopper, cart: Cart | None, rng: random.Random,
                    dt_s: float) -> None:
    """Shoppers shadow their cart with a wandering offset; free walk otherwise."""
    if cart is not None:
        tx = cart.x + rng.uniform(-1.2, 1.2)
        ty = cart.y + rng.uniform(-1.2, 1.2)
    else:
        tx = shopper.x + rng.uniform(-2.0, 2.0)
        ty = shopper.y + rng.uniform(-2.0, 2.0)
    shopper.x, shopper.y, _ = step_towards(shopper.x, shopper.y, tx, ty,
                                           shopper.speed_ms * dt_s)
