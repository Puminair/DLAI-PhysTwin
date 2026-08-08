"""Layer 1 — the single authoritative clock.

May import: stdlib only.

One clock, injected everywhere. Never call time.time() outside this
module. Three unsynchronised clocks is the failure mode this whole
project is built to avoid. All timestamps are UTC milliseconds.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Clock:
    """Simulation clock. Advances only when told to.

    epoch_ms is the UTC-millisecond timestamp of simulation start; every
    emitted time is epoch_ms + elapsed simulated time. Deterministic by
    construction — wall time never leaks in.
    """

    epoch_ms: int = 1_754_600_000_000  # fixed sim epoch (2025-08-07T21:33:20Z)
    _elapsed_ms: int = field(default=0, repr=False)

    def now_ms(self) -> int:
        return self.epoch_ms + self._elapsed_ms

    def elapsed_s(self) -> float:
        return self._elapsed_ms / 1000.0

    def advance(self, dt_s: float) -> None:
        if dt_s < 0:
            raise ValueError("clock never goes backwards")
        self._elapsed_ms += round(dt_s * 1000)
