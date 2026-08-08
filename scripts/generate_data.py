"""Generate the parametric data/ inputs for the digital twin.

Layer: none (offline tooling; imports config only).

CLAUDE.md describes the data/ files as locked inputs, but the repository
was created empty — no data/ directory exists anywhere in its history.
This script therefore *creates* them, deterministically (fixed seed),
from the locked constants in config/defaults.yaml. Every output carries
a provenance block naming this script, the seed, and whether each source
constant is verified or assumed. Interior fit-out is a parametric model,
not a survey (CLAUDE.md §9) — re-running this script reproduces the
byte-identical dataset.

Usage: python scripts/generate_data.py [--out data/]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402

SEED = 20140457  # building-permit number: arbitrary but memorable and fixed
DATASET_VERSION = "1.0-parametric"


def provenance(note: str) -> dict:
    return {
        "generator": "scripts/generate_data.py",
        "dataset_version": DATASET_VERSION,
        "seed": SEED,
        "note": note,
    }


def rect(cx: float, cy: float, w: float, h: float) -> list[list[float]]:
    """Axis-aligned rectangle ring (closed) around centre (cx, cy)."""
    hw, hh = w / 2.0, h / 2.0
    return [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh],
            [cx - hw, cy + hh], [cx - hw, cy - hh]]


def point_in_ring(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring) - 1  # closed ring
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def feature(fid: str, kind: str, material: str, ring: list[list[float]],
            z0: float, z1: float, zone: str, prov_note: str) -> dict:
    return {
        "type": "Feature",
        "id": fid,
        "properties": {
            "kind": kind,
            "material": material,
            "z0_m": z0,
            "z1_m": z1,
            "zone": zone,
            "provenance": prov_note,
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def build_layer1(cfg) -> dict:
    """Layer-1 GeoJSON: envelope, walls, fixtures, checkout, gates, docks."""
    g = cfg.section("geometry")
    env_ring = [list(p) for p in cfg.get("envelope.ring")]
    env_ring.append(list(env_ring[0]))
    ceiling = cfg.get("envelope.ceiling_m")
    x_split = cfg.get("areas.x_split_m")
    feats: list[dict] = []

    feats.append(feature("envelope", "envelope", "concrete", env_ring,
                         0.0, ceiling, "all", "verified: permit 20140457"))
    # Split wall sales / back-of-house, with two doorway openings.
    for i, (y0, y1) in enumerate([(0.0, 18.0), (20.0, 36.0), (38.0, 55.0)]):
        feats.append(feature(f"wall_split_{i}", "wall", "wall",
                             rect(x_split, (y0 + y1) / 2, 0.20, y1 - y0),
                             0.0, ceiling, "boundary", "derived: x_split solves sales area"))

    # Gondola runs: between adjacent aisle centrelines, segmented into bays.
    y_start, pitch = g["aisle_y_start_m"], g["aisle_pitch_m"]
    n_rows = g["aisles"] - 1  # gondolas sit between the 10 aisle centrelines
    x0, run, depth, height, bay = (g["aisle_x_start_m"], g["aisle_run_m"],
                                   g["gondola_depth_m"], g["gondola_height_m"],
                                   g["gondola_bay_m"])
    n_bays = int(run // bay)
    for r in range(n_rows):
        cy = y_start + pitch / 2 + r * pitch
        for b in range(n_bays):
            cx = x0 + bay / 2 + b * bay
            feats.append(feature(f"gondola_r{r:02d}_b{b:02d}", "gondola", "gondola",
                                 rect(cx, cy, bay, depth), 0.0, height,
                                 "sales_floor", "assumed: parametric fit-out"))

    # Columns: 8 m grid clipped to envelope -> exactly 77 (CLAUDE.md §2).
    csz = g["column_size_m"]
    n_col = 0
    for gx in range(4, 100, 8):
        for gy in range(4, 56, 8):
            if point_in_ring(gx, gy, env_ring):
                feats.append(feature(f"column_{n_col:02d}", "column", "column",
                                     rect(gx, gy, csz, csz), 0.0, ceiling,
                                     "sales_floor" if gx < x_split else "back_of_house",
                                     "assumed: 8 m structural grid"))
                n_col += 1

    # End-caps at both ends of every gondola row, and promo pallets in the
    # aisles: they are what breaks along-aisle line-of-sight at panel height.
    for r in range(n_rows):
        cy = y_start + pitch / 2 + r * pitch
        for side, ex in (("w", x0 - 0.7), ("e", x0 + run + 0.7)):
            feats.append(feature(f"endcap_{side}_r{r:02d}", "endcap", "gondola",
                                 rect(ex, cy, 1.0, 1.2), 0.0, 1.6,
                                 "sales_floor", "assumed: end-cap display"))
    for a in range(g["aisles"]):
        ay = y_start + a * pitch
        for k, frac in enumerate((0.32, 0.68)):
            px = x0 + frac * run
            # offset to one side so a cart can still pass
            py = ay + (0.9 if (a + k) % 2 == 0 else -0.9)
            feats.append(feature(f"promo_a{a:02d}_{k}", "promo_pallet", "gondola",
                                 rect(px, py, 1.2, 1.0), 0.0, 1.6,
                                 "sales_floor", "assumed: mid-aisle promo pallet"))

    # Chillers along the north wall and the east edge of the sales floor.
    for i in range(11):
        cx = 6.0 + 3.75 / 2 + i * 3.9
        feats.append(feature(f"chiller_n_{i:02d}", "chiller", "chiller",
                             rect(cx, 54.0, 3.75, 1.1), 0.0, 2.2,
                             "sales_floor", "assumed: perimeter chiller line"))
    for i in range(10):
        cy = 6.0 + 2.0 + i * 4.4
        feats.append(feature(f"chiller_e_{i:02d}", "chiller", "chiller",
                             rect(49.6, cy, 1.1, 3.75), 0.0, 2.2,
                             "sales_floor", "assumed: perimeter chiller line"))

    # Produce tables in the front-east open area of the sales floor.
    for i in range(8):
        cx = 44.0 + (i % 2) * 3.2
        cy = 6.0 + (i // 2) * 4.0
        feats.append(feature(f"produce_{i:02d}", "produce_table", "gondola",
                             rect(cx, cy, 2.0, 1.4), 0.0, 1.2,
                             "sales_floor", "assumed: produce area"))

    # Checkouts along the west wall front runway.
    for i in range(12):
        cy = 6.0 + i * 3.6
        feats.append(feature(f"checkout_{i:02d}", "checkout", "gondola",
                             rect(3.0, cy, 2.4, 1.0), 0.0, 1.1,
                             "sales_floor", "assumed: 12 lanes, west wall"))

    # Entrance / exit gates on the south wall; cart dock beside them.
    feats.append(feature("gate_entry", "gate", "gate", rect(10.0, 0.6, 3.0, 0.4),
                         0.0, 1.2, "sales_floor", "assumed: south entrance"))
    feats.append(feature("gate_exit_0", "gate", "gate", rect(16.0, 0.6, 3.0, 0.4),
                         0.0, 1.2, "sales_floor", "assumed: south exit"))
    feats.append(feature("gate_exit_1", "gate", "gate", rect(20.0, 0.6, 3.0, 0.4),
                         0.0, 1.2, "sales_floor", "assumed: south exit"))
    feats.append(feature("cart_dock", "cart_dock", "gate", rect(5.0, 1.5, 4.0, 1.5),
                         0.0, 1.2, "sales_floor", "assumed: cart corral at entrance"))

    # Back of house: cold rooms (opaque), warehouse racking, rooms, docks.
    cold_rooms = [(56.0, 46.0, 8.0, 10.0), (66.0, 46.0, 8.0, 10.0),
                  (56.0, 34.0, 8.0, 8.0), (66.0, 34.0, 8.0, 8.0)]
    for i, (cx, cy, w, h) in enumerate(cold_rooms):
        feats.append(feature(f"cold_room_{i}", "cold_room", "cold_room",
                             rect(cx, cy, w, h), 0.0, 3.5,
                             "back_of_house", "assumed: cold chain block"))
    # Warehouse racking rows (steel), eastern block.
    for r in range(8):
        cy = 16.0 + r * 4.6
        for s in range(5):
            cx = 76.0 + s * 4.6
            if point_in_ring(cx, cy - 0.7, env_ring) and point_in_ring(cx, cy + 0.7, env_ring):
                feats.append(feature(f"rack_r{r}_s{s}", "racking", "racking",
                                     rect(cx, cy, 4.2, 1.4), 0.0, 4.0,
                                     "warehouse", "assumed: pallet racking"))
    rooms = [("prep_meat", 54.0, 24.0, 6.0, 8.0), ("prep_bakery", 54.0, 12.0, 6.0, 8.0),
             ("prep_deli", 62.0, 24.0, 6.0, 8.0), ("office", 62.0, 12.0, 6.0, 6.0),
             ("plant_room", 70.0, 24.0, 6.0, 8.0), ("staff_room", 70.0, 14.0, 6.0, 6.0)]
    for name, cx, cy, w, h in rooms:
        feats.append(feature(name, "room", "wall", rect(cx, cy, w, h),
                             0.0, 3.0, "back_of_house", "assumed: BOH rooms"))
    for i in range(4):
        cy = 32.0 + i * 6.0
        feats.append(feature(f"dock_{i}", "dock", "gate", rect(99.0, cy, 1.8, 3.0),
                             0.0, 3.0, "warehouse", "assumed: east loading docks"))

    return {
        "type": "FeatureCollection",
        "name": "store_layer1",
        "provenance": provenance(
            f"parametric fit-out; {len(feats)} entities "
            "(spec target 436 refers to the surveyed dataset this model stands in for)"),
        "crs_note": "local metres, origin SW corner of leased block, X east Y north Z up; "
                    "floor at -6.70 m relative to complex datum",
        "features": feats,
    }


def build_sensors(cfg, env_ring) -> dict:
    """150 logical sensor positions: perimeter + sales grid + BOH grid."""
    s = cfg.section("sensors")
    x_split = cfg.get("areas.x_split_m")
    mount_z = s["mount_z_m"]
    sensors: list[dict] = []

    def add(zone: str, x: float, y: float):
        sensors.append({
            "sensor_id": f"S{len(sensors):03d}",
            "x": round(x, 2), "y": round(y, 2), "z": mount_z,
            "zone": zone, "model": s["model"],
            "units": s["units_per_logical"],
        })

    # 18 perimeter units, evenly spaced along the envelope ring, 1 m inboard.
    ring = env_ring[:-1]
    per = []
    total = sum(math.dist(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring)))
    step = total / s["perimeter_units"]
    acc, target = 0.0, step / 2
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        seg = math.dist(a, b)
        while target <= acc + seg:
            t = (target - acc) / seg
            px, py = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            # nudge 1 m inboard toward the centroid
            cx0, cy0 = 50.0, 27.5
            d = math.hypot(cx0 - px, cy0 - py)
            per.append((px + (cx0 - px) / d, py + (cy0 - py) / d))
            target += step
        acc += seg
    for px, py in per[:s["perimeter_units"]]:
        add("perimeter", px, py)

    def grid_fill(zone: str, x0, x1, y0, y1, n: int):
        """Deterministic grid sized to yield >= n candidates, truncated to n."""
        area = (x1 - x0) * (y1 - y0)
        pitch = math.sqrt(area / n)
        while True:
            nx = int((x1 - x0) / pitch)
            ny = int((y1 - y0) / pitch)
            pts = [(x0 + (i + 0.5) * (x1 - x0) / nx, y0 + (j + 0.5) * (y1 - y0) / ny)
                   for j in range(ny) for i in range(nx)
                   if point_in_ring(x0 + (i + 0.5) * (x1 - x0) / nx,
                                    y0 + (j + 0.5) * (y1 - y0) / ny, env_ring)]
            if len(pts) >= n:
                break
            pitch *= 0.97
        for px, py in pts[:n]:
            add(zone, px, py)

    grid_fill("sales_floor", 2.0, x_split - 1.0, 2.0, 53.0, s["sales_units"])
    grid_fill("back_of_house", x_split + 1.0, 99.0, 2.0, 53.0, s["boh_units"])

    assert len(sensors) == s["logical"], f"expected {s['logical']}, got {len(sensors)}"
    return {
        "provenance": provenance("perimeter 18 + sales 104 + BOH 28 = 150 logical positions; "
                                 "BOH under-provisioned by design of v0.2 quotas"),
        "mount_z_m": mount_z,
        "sensors": sensors,
    }


def build_scene3d(layer1: dict, sensors: dict, cfg) -> dict:
    solids = []
    for f in layer1["features"]:
        p = f["properties"]
        if f["id"] == "envelope":
            continue
        ring = f["geometry"]["coordinates"][0]
        solids.append({"id": f["id"], "kind": p["kind"], "material": p["material"],
                       "ring": ring, "z0": p["z0_m"], "z1": p["z1_m"]})
    openings = [
        {"id": "door_split_a", "at": {"x": cfg.get("areas.x_split_m"), "y": 19.0}, "w": 2.0, "h": 2.4},
        {"id": "door_split_b", "at": {"x": cfg.get("areas.x_split_m"), "y": 37.0}, "w": 2.0, "h": 2.4},
        {"id": "entrance", "at": {"x": 10.0, "y": 0.0}, "w": 3.0, "h": 2.4},
    ]
    return {
        "provenance": provenance("extruded from store_layer1.geojson"),
        "envelope": {"ring": cfg.get("envelope.ring"),
                     "ceiling_m": cfg.get("envelope.ceiling_m"),
                     "floor_level_m": cfg.get("envelope.floor_level_m")},
        "solids": solids,
        "openings": openings,
        "sensors": sensors["sensors"],
    }


def write_stl(scene3d: dict, path: Path) -> int:
    """Binary STL of all extruded solids (12 triangles per box footprint edge-pair)."""
    tris: list[tuple] = []

    def quad(a, b, c, d):
        tris.append((a, b, c))
        tris.append((a, c, d))

    for s in scene3d["solids"]:
        ring = s["ring"][:-1]
        z0, z1 = s["z0"], s["z1"]
        n = len(ring)
        # side walls
        for i in range(n):
            (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
            quad((x1, y1, z0), (x2, y2, z0), (x2, y2, z1), (x1, y1, z1))
        # top and bottom fans (rings are convex rects here)
        for j in range(1, n - 1):
            tris.append(((*ring[0], z1), (*ring[j], z1), (*ring[j + 1], z1)))
            tris.append(((*ring[0], z0), (*ring[j + 1], z0), (*ring[j], z0)))
    # envelope walls
    env = scene3d["envelope"]["ring"]
    zc = scene3d["envelope"]["ceiling_m"]
    for i in range(len(env)):
        (x1, y1), (x2, y2) = env[i], env[(i + 1) % len(env)]
        quad((x1, y1, 0.0), (x2, y2, 0.0), (x2, y2, zc), (x1, y1, zc))

    with open(path, "wb") as fh:
        fh.write(b"centro_scene parametric".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            ux = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            vx = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            n = (ux[1] * vx[2] - ux[2] * vx[1],
                 ux[2] * vx[0] - ux[0] * vx[2],
                 ux[0] * vx[1] - ux[1] * vx[0])
            mag = math.sqrt(sum(v * v for v in n)) or 1.0
            fh.write(struct.pack("<3f", *(v / mag for v in n)))
            for p in (a, b, c):
                fh.write(struct.pack("<3f", *p))
            fh.write(struct.pack("<H", 0))
    return len(tris)


def build_rf_table(cfg) -> dict:
    """CW9172I Tx/Rx per MCS per band — transcribed-shape table.

    Values follow the CW9172I datasheet pattern (Tx falls with MCS order,
    Rx sensitivity rises); marked assumed because the primary PDF is not
    in this repository to transcribe from.
    """
    bands = {
        "2.4": {"tx_max_dbm": 20.0, "rx_floor_dbm": -94.0},
        "5": {"tx_max_dbm": 19.0, "rx_floor_dbm": -92.0},
        "6": {"tx_max_dbm": 18.0, "rx_floor_dbm": -91.0},
    }
    mcs_rows = []
    for band, b in bands.items():
        for mcs in range(12 if band != "2.4" else 8):
            mcs_rows.append({
                "band": band, "mcs": mcs,
                "tx_dbm": round(b["tx_max_dbm"] - 0.5 * max(0, mcs - 4), 1),
                "rx_sensitivity_dbm": round(b["rx_floor_dbm"] + 3.0 * mcs, 1),
            })
    return {"provenance": provenance("assumed: datasheet-shaped table; verify against "
                                     "CW9172I primary PDF before relying on absolute values"),
            "antenna_gain_dbi": cfg.get("sensors.antenna_gain_dbi"),
            "mcs_table": mcs_rows}


def build_device_models(cfg) -> dict:
    return {
        "provenance": provenance("assumed: cart panel Tx power is a class value; "
                                 "no vendor figure exists (CLAUDE.md §9)"),
        "cart_panel": {
            "eirp_dbm": cfg.get("cart_uplink.eirp_dbm"),
            "antenna_z_m": cfg.get("heights.cart_panel_z_m"),
            "bands": ["2.4", "5"],
            "mac_policy": "stable_per_device",
            "beacon_interval_s": 3.0,
        },
        "pos_terminal": {"eirp_dbm": {"2.4": 15.0, "5": 14.0}, "antenna_z_m": 0.9,
                         "mac_policy": "stable_per_device", "wired": True},
        "gate_controller": {"eirp_dbm": {"2.4": 14.0}, "antenna_z_m": 1.1,
                            "mac_policy": "stable_per_device", "wired": True},
        "shopper_phone": {"eirp_dbm": {"2.4": 14.0, "5": 13.0}, "antenna_z_m": 1.1,
                          "mac_policy": "randomized_unassociated"},
    }


def build_aireye_spec() -> dict:
    return {
        "provenance": provenance("assumed: alert taxonomy coverage map"),
        "alert_taxonomy": [
            {"signature": "rogue_ap_on_wire", "stream": "air_marshal",
             "carries_client_identity": True, "key_field": "wiredMacs"},
            {"signature": "honeypot_ssid", "stream": "air_marshal",
             "carries_client_identity": False, "key_field": "ssid"},
            {"signature": "deauth_flood", "stream": "awips_syslog",
             "carries_client_identity": False,
             "throttling": "one message per signature per AP per interval"},
            {"signature": "beacon_spoof", "stream": "awips_syslog",
             "carries_client_identity": False,
             "throttling": "one message per signature per AP per interval"},
            {"signature": "containment_active", "stream": "air_marshal",
             "carries_client_identity": False,
             "side_effect": "scanning radio time-splits; RTLS accuracy degrades"},
        ],
        "neighbour_networks_same_floor_or_above": 5,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    random.seed(SEED)
    cfg = load_config()
    env_ring = [list(p) for p in cfg.get("envelope.ring")]
    env_ring.append(list(env_ring[0]))

    layer1 = build_layer1(cfg)
    sensors = build_sensors(cfg, env_ring)
    scene3d = build_scene3d(layer1, sensors, cfg)

    def dump(name: str, obj: dict):
        with open(out / name, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1)
        print(f"wrote {name}")

    dump("store_layer1.geojson", layer1)
    dump("sensing_layer2.json", sensors)
    dump("store_scene3d.json", scene3d)
    n_tris = write_stl(scene3d, out / "centro_scene.stl")
    print(f"wrote centro_scene.stl ({n_tris} triangles)")
    dump("cw9172i_rf_table.json", build_rf_table(cfg))
    dump("material_penetration.json", {
        "provenance": provenance("ITU-R P.2040-3 equivalent-thickness computation; "
                                 "gondola value is the most sensitive parameter in the "
                                 "model — first thing to measure (CLAUDE.md §9)"),
        "attenuation_db": cfg.section("attenuation_db")})
    dump("device_models.json", build_device_models(cfg))
    dump("aireye_emulator_spec.json", build_aireye_spec())
    print(f"entities in store_layer1.geojson: {len(layer1['features'])}")


if __name__ == "__main__":
    main()
