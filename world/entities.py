"""Layer 1 — entities that exist in the physical world.

May import: stdlib, world.clock.

Every entity carries a stable ``id`` that exists in the world, plus a
``provenance`` field recording how it came to exist. The identity trap
(CLAUDE.md §0): a cart has a cart_id — a fact. It does NOT have a MAC
here; a MAC is an observation and belongs to Layer 2. If cart_id and mac
ever sit in the same record, the separation has collapsed.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class CartState(enum.Enum):
    DOCKED = "docked"
    SHOPPING = "shopping"
    QUEUING = "queuing"
    PAYING = "paying"
    EXITING = "exiting"


@dataclass
class Cart:
    id: str
    provenance: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0                       # wheel plane; panel height is a config value
    state: CartState = CartState.DOCKED
    speed_ms: float = 0.8
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    dwell_until_s: float = 0.0
    items: int = 0


@dataclass
class Shopper:
    id: str
    provenance: str
    x: float = 0.0
    y: float = 0.0
    cart_id: str | None = None           # a world fact: this shopper pushes this cart
    speed_ms: float = 1.0


@dataclass
class Staff:
    """An employee on shift. A world fact — role, post, position.

    Like every Layer-1 entity: no MAC, no RF identity. Their phone's MAC
    is an observation and lives in sensing/pipeline.py. Staff bodies
    absorb RF exactly like shoppers' — Layer 2 observes them as bodies.
    """
    id: str
    provenance: str
    role: str                            # cashier | stocker | warehouse | prep | security | manager
    x: float = 0.0
    y: float = 0.0
    post_x: float = 0.0                  # home position for stationary roles
    post_y: float = 0.0
    speed_ms: float = 1.0
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    dwell_until_s: float = 0.0


@dataclass
class Checkout:
    id: str
    provenance: str
    x: float = 0.0
    y: float = 0.0
    open: bool = True
    queue: list[str] = field(default_factory=list)   # cart ids, front first
    busy_until_s: float = 0.0


@dataclass
class Gate:
    id: str
    provenance: str
    x: float = 0.0
    y: float = 0.0
    direction: str = "exit"              # "entry" | "exit"


@dataclass
class Dock:
    id: str
    provenance: str
    x: float = 0.0
    y: float = 0.0


@dataclass
class Product:
    id: str
    provenance: str
    gondola_id: str = ""
