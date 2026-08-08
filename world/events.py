"""Layer 1 — world events with true timestamps.

May import: stdlib, world.clock.

Events are facts: they carry the entity's world id and the authoritative
clock's timestamp. They are what the blind test scores Layer 3 against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from world.clock import Clock

EVENT_TYPES = frozenset({
    "cart_undocked", "item_picked", "payment_started", "payment_completed",
    "cart_exited_gate", "cart_docked", "fault",
})


@dataclass(frozen=True)
class WorldEvent:
    t_ms: int              # authoritative clock, UTC milliseconds
    type: str
    entity_id: str
    x: float
    y: float
    detail: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """Append-only trace of world events, in clock order."""

    def __init__(self, clock: Clock):
        self._clock = clock
        self.events: list[WorldEvent] = []

    def emit(self, type_: str, entity_id: str, x: float, y: float,
             **detail: Any) -> WorldEvent:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        ev = WorldEvent(t_ms=self._clock.now_ms(), type=type_,
                        entity_id=entity_id, x=x, y=y, detail=dict(detail))
        self.events.append(ev)
        return ev
