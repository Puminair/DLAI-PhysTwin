"""Run an attack from the catalogue against the twin and watch DLAI react.

Layer: demonstration harness (may import world, sensing, dlai — like
eval/). It closes the loop the rest of the repo builds piece by piece:
pick an attack from config/attack_catalog.yaml, inject it into the twin,
and print exactly what Layer 3 does with it — the assessment, the attack
path, and the RECOMMEND_ONLY action — or, for a structural blind spot,
the honest "DLAI cannot see this, and here is why".

Injection is faithful to each attack's nature:
  * cart_mac_clone runs the REAL physical twin (WorldSim) through the
    REAL sensing pipeline, then clones a real cart panel's MAC to a far
    position — a physical attack observed by real Layer 2, resolved by
    Layer 3.
  * network / RF attacks are emitted as Layer-2 security records (the
    same shapes sensing/ produces) and routed through the DLAI attack
    analyzer and alert engine.
  * blind-spot attacks produce no detection by design; the runner shows
    the catalogue's reason and the mitigation instead of faking one.

Usage:
  python scripts/run_attack.py --list
  python scripts/run_attack.py --attack rogue_ap_on_wire
  python scripts/run_attack.py --attack cart_mac_clone
  python scripts/run_attack.py --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from config import load_config                              # noqa: E402
from dlai.alerts import AlertEngine                         # noqa: E402
from dlai.attack import AttackAnalyzer                      # noqa: E402
from dlai.entity import EntityResolver                      # noqa: E402
from dlai.floorplan import FloorPlan                        # noqa: E402
from dlai.ingest import (NormalisedObservation,             # noqa: E402
                         load_sensor_sites)
from dlai.interaction import classify_track                 # noqa: E402
from dlai.policy import PolicyEngine                        # noqa: E402
from sensing.observation import (air_marshal_record,        # noqa: E402
                                 awips_syslog_record)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
T0 = 1_754_600_000_000    # fixed demo epoch (ms) — deterministic

# ── formatting ────────────────────────────────────────────────────────────
def line(ch="─", n=74):
    return ch * n


# which layers of the twin each demo actually traverses
STAGES = {
    "cart_mac_clone": "L1 world → L2 sensing → L3 DLAI   (full physical stack)",
    "exit_without_checkout": "L2 observations → L3 DLAI (entity → policy)",
    "rogue_ap_on_wire": "L2 sensing (Air Marshal) → L3 DLAI",
    "rogue_ap_on_pos_vlan": "L2 sensing (Air Marshal) → L3 DLAI",
    "evil_twin_ops_ssid": "L2 sensing (Air Marshal) → L3 DLAI",
    "containment_abuse": "L2 sensing (Air Marshal) → L3 DLAI",
    "ip_camera_as_pivot": "L2 sensing (Air Marshal) → L3 DLAI",
    "deauth_flood": "L2 sensing (aWIPS syslog) → L3 DLAI",
}


def head(attack: dict):
    print(line("═"))
    print(f"ATTACK   {attack['name']}")
    print(f"         id={attack['id']}  ·  {attack['category']}  ·  "
          f"severity={attack['severity']}")
    print(f"         target: {attack['target']}")
    stage = STAGES.get(attack["id"])
    if stage:
        print(f"PIPELINE {stage}")
    print(line())


def show_alerts(alerts):
    if not alerts:
        print("DLAI     (no alert emitted)")
        return
    for a in alerts:
        print(f"ALERT    {a.alert_id}  [{a.severity}]  confidence={a.confidence}"
              f"  mode={a.mode}")
        print(f"         → {a.action}")
        if a.blind_spots:
            print(f"         blind spots: {'; '.join(a.blind_spots)}")


def show_blind(attack: dict):
    print("DLAI     BLIND SPOT — this attack is not detectable by the RF twin.")
    print(f"         why: {attack.get('blind_spot_reason','(structural)')}")
    print(f"         mitigation: {attack['mitigation']}")


# ── injection handlers ────────────────────────────────────────────────────
def _run_security_record(record: dict):
    """Route one Layer-2 security record through DLAI, return alerts."""
    analyzer, engine = AttackAnalyzer(), AlertEngine()
    assessment = analyzer.consume(record)
    stream = record.get("stream")
    print(f"INJECT   Layer 2 · {stream}")
    if assessment is not None:
        print(f"DLAI     assessment: {assessment.kind}  "
              f"(confidence={assessment.confidence})")
        if assessment.attack_path:
            print(f"         attack path: {' → '.join(assessment.attack_path)}")
        engine.consume_assessment(assessment)
    return engine.evaluate()


def atk_rogue_on_wire(cfg):
    rec = air_marshal_record(
        bssid="de:ad:be:ef:00:01", ssid="centro-ops", channel=6,
        first_seen_ms=T0 - 120_000, last_seen_ms=T0,
        wired_macs=["00:50:56:aa:bb:cc"], wired_vlans=[99],
        manufacturer="Espressif", encryption="open", contained=False, gaps=[])
    print("INJECT   rogue AP seen on the wire (wiredMacs present)")
    return _run_security_record(rec)


def atk_rogue_pos_vlan(cfg):
    rec = air_marshal_record(
        bssid="de:ad:be:ef:00:02", ssid="centro-ops", channel=6,
        first_seen_ms=T0 - 120_000, last_seen_ms=T0,
        wired_macs=["00:50:56:aa:bb:cd"], wired_vlans=[12],   # POS VLAN
        manufacturer="Espressif", encryption="open", contained=False, gaps=[])
    print("INJECT   rogue AP bridged onto the POS VLAN (wiredVlans=[12])")
    return _run_security_record(rec)


def atk_evil_twin(cfg):
    rec = air_marshal_record(
        bssid="a1:b2:c3:d4:e5:f6", ssid="centro-ops", channel=11,
        first_seen_ms=T0 - 120_000, last_seen_ms=T0,
        wired_macs=[], wired_vlans=[],           # no wire evidence → caps at warn
        manufacturer="Unknown", encryption="open", contained=False, gaps=[])
    print("INJECT   evil-twin of the ops SSID, NO wire evidence")
    return _run_security_record(rec)


def atk_deauth(cfg):
    rec = awips_syslog_record(signature="deauth_flood", ap_mac="00:2a:10:00:00:06",
                              t_ms=T0, suppressed_count=999)
    print("INJECT   deauth flood — aWIPS signature (intensity throttled away)")
    return _run_security_record(rec)


def atk_containment(cfg):
    rec = air_marshal_record(
        bssid="de:ad:be:ef:00:03", ssid="rogue-x", channel=6,
        first_seen_ms=T0 - 120_000, last_seen_ms=T0,
        wired_macs=[], wired_vlans=[], manufacturer="Various",
        encryption="wpa2", contained=True,
        gaps=["containment_rtls_degraded"])
    print("INJECT   Air Marshal containment ACTIVE (scanning radio time-splits)")
    return _run_security_record(rec)


def atk_ip_camera_pivot(cfg):
    rec = air_marshal_record(
        bssid="cc:cc:cc:00:00:01", ssid="centro-ops", channel=6,
        first_seen_ms=T0 - 120_000, last_seen_ms=T0,
        wired_macs=["b8:a4:4f:11:22:33"], wired_vlans=[40],   # camera VLAN
        manufacturer="Hikvision", encryption="open", contained=False, gaps=[])
    print("INJECT   compromised IP camera presenting a bridge on the wire")
    return _run_security_record(rec)


def atk_cart_mac_clone(cfg):
    """The physical-twin path: run the real world through real sensing, then
    clone a real cart panel MAC to a far position and let DLAI catch it."""
    from sensing.pipeline import ObservationPipeline
    from sensing.sensor import SensorField, load_sensors
    from world.geometry import StoreGeometry
    from world.sim import WorldSim
    from dlai.ingest import normalise_batch

    geo = StoreGeometry(DATA / "store_layer1.geojson")
    field = SensorField(units=load_sensors(DATA / "sensing_layer2.json"),
                        geometry=geo, cfg=cfg)
    sim = WorldSim(geometry=geo, cfg=cfg, seed=7, arrival_rate_per_min=4.0)
    pipe = ObservationPipeline(field_=field, cfg=cfg)
    for i in range(600):                     # 5 sim minutes
        sim.step(0.5)
        if i % 10 == 9:
            pipe.observe(sim.snapshot())
    pipe.flush_all(sim.clock.now_ms())

    # find a real cart panel observation that got a cloud fix
    victim = None
    for batch in pipe.delivered:
        for obs in normalise_batch(batch.to_json()):
            if obs.mac.startswith("0c:8b:7d") and obs.cloud_location:
                victim = obs
                break
        if victim:
            break
    if victim is None:
        print("INJECT   (no located cart panel this run — try another seed)")
        return []
    print(f"INJECT   physical twin ran; real panel {victim.mac} located at "
          f"({victim.cloud_location['x']}, {victim.cloud_location['y']})")
    print("         → cloning that MAC onto a device 45 m away, same instant")
    engine = AlertEngine()
    engine.consume_observation(victim)
    clone = NormalisedObservation(
        mac=victim.mac, seen_time_ms=victim.seen_time_ms + 1000,
        delivered_at_ms=victim.delivered_at_ms + 1000, rssi_dbm=[],
        cloud_location={"x": victim.cloud_location["x"] + 45.0,
                        "y": victim.cloud_location["y"], "variance": 6.0},
        gaps=[])
    engine.consume_observation(clone)
    print("DLAI     entity resolution: one MAC, two positions 45 m apart in 1 s")
    return engine.evaluate()


def atk_exit_without_checkout(cfg):
    """Craft a track that reaches an exit with no checkout presence."""
    sites = load_sensor_sites(DATA / "sensing_layer2.json")
    plan = FloorPlan(DATA / "store_layer1.geojson")
    resolver = EntityResolver(sites, plan)
    mac = "0c:8b:7d:00:00:aa"
    macs = list(sites)[:5]
    # a short path ending in the exit-gate band (y <= 3), never at a checkout
    for k, (x, y) in enumerate([(20, 20), (16, 12), (12, 6), (12, 2.0)]):
        resolver.consume(NormalisedObservation(
            mac=mac, seen_time_ms=T0 + k * 4000, delivered_at_ms=T0 + k * 4000,
            rssi_dbm=[(m, -50.0 - i) for i, m in enumerate(macs)],
            cloud_location={"x": x, "y": y, "variance": 3.0}, gaps=[]))
    track = resolver.tracks[mac]
    policy = PolicyEngine()
    engine = AlertEngine()
    print("INJECT   cart track reaches the exit gate with no checkout presence")
    for rec in policy.evaluate_track(track, classify_track(track)):
        print(f"DLAI     policy: {rec.rule}  (mode={rec.mode})")
        engine.consume_recommendation(rec)
    return engine.evaluate()


HANDLERS = {
    "rogue_ap_on_wire": atk_rogue_on_wire,
    "rogue_ap_on_pos_vlan": atk_rogue_pos_vlan,
    "evil_twin_ops_ssid": atk_evil_twin,
    "deauth_flood": atk_deauth,
    "containment_abuse": atk_containment,
    "ip_camera_as_pivot": atk_ip_camera_pivot,
    "cart_mac_clone": atk_cart_mac_clone,
    "exit_without_checkout": atk_exit_without_checkout,
}


# ── driver ────────────────────────────────────────────────────────────────
def load_catalog() -> dict:
    with open(ROOT / "config" / "attack_catalog.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run_one(attack: dict, cfg) -> None:
    head(attack)
    handler = HANDLERS.get(attack["id"])
    if handler is not None:
        alerts = handler(cfg)
        show_alerts(alerts)
    elif attack["blind_spot"]:
        show_blind(attack)
    else:
        # detectable in the catalogue but no live demo wired — say so plainly
        print("DLAI     (detectable; no live-injection demo wired for this id)")
        print(f"         would surface via: {', '.join(attack['detected_by'])}")
    print()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attack", help="attack id from config/attack_catalog.yaml")
    ap.add_argument("--all", action="store_true", help="run every wired demo")
    ap.add_argument("--list", action="store_true", help="list attack ids")
    args = ap.parse_args(argv)
    cfg = load_config()
    cat = load_catalog()
    by_id = {a["id"]: a for a in cat["attacks"]}

    if args.list or (not args.attack and not args.all):
        print(f"Attack catalogue — {cat['meta']['site']}  (posture "
              f"{cat['meta']['posture']})\n")
        for a in cat["attacks"]:
            live = "▶ live demo" if a["id"] in HANDLERS else (
                "⦸ blind spot" if a["blind_spot"] else "· detected")
            print(f"  {a['id']:<32} [{a['severity']:<8}] {live}")
        print("\nrun one:  python scripts/run_attack.py --attack <id>")
        print("run all:  python scripts/run_attack.py --all")
        return

    if args.all:
        detected = blind = 0
        for a in cat["attacks"]:
            run_one(a, cfg)
            if a["id"] in HANDLERS:
                detected += 1
            elif a["blind_spot"]:
                blind += 1
        print(line("═"))
        print(f"SUMMARY  {len(cat['attacks'])} attack classes · "
              f"{detected} driven live through DLAI · "
              f"{blind} structural blind spots shown honestly")
        print("         posture RECOMMEND_ONLY throughout — DLAI recommends, "
              "never enforces")
        return

    a = by_id.get(args.attack)
    if a is None:
        raise SystemExit(f"unknown attack id: {args.attack!r} (see --list)")
    run_one(a, cfg)


if __name__ == "__main__":
    main()
