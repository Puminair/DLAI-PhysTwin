"""Layer 2 — first-order reflections by the image method. Reference impl.

May import: stdlib, config, world.geometry, sensing.propagation.

Mirror the sensor across each planar reflector surface, trace the image
ray, apply Fresnel loss at the incidence angle, sum powers linearly.

Only evaluate reflections where the direct model already fails the 3-AP
rule — that is the only place a bounce can change the answer, and it
makes the full run tractable. Measured effect: +7 points at 5 GHz, zero
at 2.4 — steel gondola backs are excellent reflectors and poor
transmitters, and at 2.4 the direct paths already win.
"""
from __future__ import annotations

import math
from typing import Callable

from sensing.propagation import fspl_db, crossing_loss_db

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]


def mirror_across_edge(p: Point2, e0: Point2, e1: Point2) -> Point2 | None:
    """Mirror point p across the infinite line through edge e0-e1."""
    ex, ey = e1[0] - e0[0], e1[1] - e0[1]
    L2 = ex * ex + ey * ey
    if L2 < 1e-12:
        return None
    t = ((p[0] - e0[0]) * ex + (p[1] - e0[1]) * ey) / L2
    fx, fy = e0[0] + t * ex, e0[1] + t * ey
    return (2 * fx - p[0], 2 * fy - p[1])


def fresnel_loss_db(incidence_cos: float, normal_loss_db: float) -> float:
    """Reflection loss vs incidence: grazing reflects better than normal."""
    return normal_loss_db * max(0.1, abs(incidence_cos))


def reflected_rssi_dbm(*, band: str, device_eirp_dbm: float,
                       sensor_antenna_gain_dbi: float, diversity_gain_db: float,
                       sensor_xyz: Point3, device_xyz: Point3,
                       reflectors: list[tuple[Point2, Point2, float]],
                       segment_crossings: Callable, attenuation_db: dict,
                       fresnel_normal_db: float,
                       max_reflectors: int = 40) -> float | None:
    """Strongest achievable RSSI summing first-order bounces (power sum).

    `reflectors` are (edge_a, edge_b, top_z) vertical faces. The image
    ray is split at the bounce point; both halves are charged their own
    material crossings (excluding nothing — the reflector's own body is
    what the crossing test sees, which is why grazing bounces along an
    aisle work and through-the-shelf ones do not, minus one crossing for
    the reflecting face itself).
    """
    sx, sy, sz = sensor_xyz
    dx, dy, dz = device_xyz
    # nearest reflectors first — the tractability trick
    scored = sorted(reflectors,
                    key=lambda r: math.hypot((r[0][0] + r[1][0]) / 2 - dx,
                                             (r[0][1] + r[1][1]) / 2 - dy))
    linear_sum = 0.0
    for e0, e1, top_z in scored[:max_reflectors]:
        img = mirror_across_edge((sx, sy), e0, e1)
        if img is None:
            continue
        # bounce point: intersection of device->image with the edge segment
        ix, iy = img
        vx, vy = ix - dx, iy - dy
        ex, ey = e1[0] - e0[0], e1[1] - e0[1]
        den = vx * ey - vy * ex
        if abs(den) < 1e-12:
            continue
        s = ((e0[0] - dx) * ey - (e0[1] - dy) * ex) / den
        if abs(ex) > abs(ey):
            u = (dx + s * vx - e0[0]) / ex
        else:
            u = (dy + s * vy - e0[1]) / ey
        if not (0.0 < s < 1.0 and 0.0 <= u <= 1.0):
            continue
        bx, by = dx + s * vx, dy + s * vy
        bz = dz + s * (sz - dz)
        if bz > top_z:               # ray passes over the reflector, no bounce
            continue
        path_m = math.hypot(ix - dx, iy - dy) or 0.1
        path_m = math.hypot(path_m, sz - dz)
        # incidence angle vs edge normal
        nx, ny = -ey, ex
        nlen = math.hypot(nx, ny) or 1.0
        vlen = math.hypot(vx, vy) or 1.0
        inc_cos = abs((vx * nx + vy * ny) / (nlen * vlen))
        # material loss on both halves, forgiving the reflecting fixture once
        c1 = segment_crossings((dx, dy, dz), (bx, by, bz))
        c2 = segment_crossings((bx, by, bz), (sx, sy, sz))
        crossings = c1 + c2
        for own in ("gondola", "racking"):
            if crossings.get(own, 0) > 0:
                crossings[own] -= 1
                break
        material = crossing_loss_db(crossings, band, attenuation_db)
        if material is None:
            continue
        rssi = (device_eirp_dbm + sensor_antenna_gain_dbi + diversity_gain_db
                - fspl_db(path_m, band) - material
                - fresnel_loss_db(inc_cos, fresnel_normal_db))
        linear_sum += 10.0 ** (rssi / 10.0)
    if linear_sum <= 0.0:
        return None
    return 10.0 * math.log10(linear_sum)
