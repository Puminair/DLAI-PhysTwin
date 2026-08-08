"""Layer 2 — RF propagation: FSPL + ITU-R P.2040-3 crossings + body loss.

May import: stdlib, config, world.geometry (to observe occlusion).

The link that matters is the UPLINK: location is computed from frames
the sensor hears FROM the cart (~16-17 dBm EIRP), not what the sensor
transmits (~24). Modelling the downlink is wrong in the optimistic
direction — every budget here starts from the device's EIRP.

RSSI = EIRP + antenna_gain + diversity − FSPL(d, f) − Σ crossings − body_loss
FSPL = 20·log10(d_m) + 20·log10(f_MHz) − 27.55

Pure functions, no hidden state. Any crossing of an opaque material
(attenuation null, e.g. cold_room — metal is a conductor, not a lossy
dielectric) kills the link outright.
"""
from __future__ import annotations

import math
from collections import Counter

BAND_MHZ = {"2.4": 2437.0, "5": 5300.0, "6": 6265.0}


def fspl_db(distance_m: float, band: str) -> float:
    d = max(distance_m, 0.1)
    return 20.0 * math.log10(d) + 20.0 * math.log10(BAND_MHZ[band]) - 27.55


def crossing_loss_db(crossings: Counter, band: str,
                     attenuation_db: dict) -> float | None:
    """Total material loss; None means an opaque material was crossed."""
    table = attenuation_db[band]
    total = 0.0
    for material, n in crossings.items():
        if n == 0:
            continue
        per = table.get(material)
        if per is None:
            return None       # opaque — no link through a conductor
        total += per * n
    return total


def bodies_crossed(a: tuple[float, float, float], b: tuple[float, float, float],
                   shoppers: list[tuple[float, float]],
                   shoulder_half_width_m: float,
                   body_top_z_m: float = 1.8) -> int:
    """Bodies the ray passes through — geometric blocking, not noise.

    A body counts if its centre lies within shoulder half-width of the
    ray's xy-projection and the ray's height at that point is below the
    top of a standing person. This varies with time as shoppers move and
    is the mechanism behind flicker.
    """
    from world.geometry import point_segment_dist
    n = 0
    a2, b2 = (a[0], a[1]), (b[0], b[1])
    for sx, sy in shoppers:
        dist, t = point_segment_dist((sx, sy), a2, b2)
        if dist <= shoulder_half_width_m:
            z_at = a[2] + t * (b[2] - a[2])
            if z_at <= body_top_z_m:
                n += 1
    return n


def uplink_rssi_dbm(*, band: str, device_eirp_dbm: float,
                    sensor_antenna_gain_dbi: float, diversity_gain_db: float,
                    distance_m: float, crossings: Counter, n_bodies: int,
                    attenuation_db: dict, body_loss_db: dict) -> float | None:
    """RSSI the sensor hears from the device. None = no link (opaque path)."""
    material = crossing_loss_db(crossings, band, attenuation_db)
    if material is None:
        return None
    return (device_eirp_dbm + sensor_antenna_gain_dbi + diversity_gain_db
            - fspl_db(distance_m, band) - material
            - body_loss_db[band] * n_bodies)
