"""Contextual alert list: catalog integrity, engine rules, RECOMMEND_ONLY.

The alerts are defined by this building's context (five neighbour
networks, throttled aWIPS, containment side effects, no inside/outside
discrimination, flicker) — these tests pin exactly those properties.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from dlai.alerts import Alert, AlertEngine
from dlai.attack import AttackAnalyzer, AttackAssessment
from dlai.ingest import NormalisedObservation
from dlai.policy import Recommendation

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "alerts.yaml"

REQUIRED_KEYS = {"id", "title", "severity", "streams", "context_conditions",
                 "evidence_required", "action", "known_blind_spots"}
REQUIRED_IDS = {
    "rogue_ap_on_wire", "rogue_ap_on_pos_vlan", "evil_twin_ops_ssid",
    "deauth_flood", "containment_active_rtls_risk", "cart_mac_duplicate",
    "cart_impossible_motion", "probe_surge_unassociated", "sensor_silent",
    "exit_without_checkout", "neighbour_network_catalogued",
}
VALID_STREAMS = {"air_marshal", "awips_syslog", "scanning_api", "dlai_track"}

# forbidden as imperative verbs anywhere an operator reads an action —
# word-boundary match so "containment" (a noun about the system's own
# behaviour) stays legal while "contain the AP" would not be.
IMPERATIVE = re.compile(r"\b(block|contain|disconnect)\b", re.IGNORECASE)


def _obs(mac: str, t: int, *, loc: dict | None = None,
         rssi: list[tuple[str, float]] | None = None,
         ssid: str | None = None, gaps: list[str] | None = None
         ) -> NormalisedObservation:
    return NormalisedObservation(
        mac=mac, seen_time_ms=t, delivered_at_ms=t,
        rssi_dbm=list(rssi or []), cloud_location=loc,
        gaps=list(gaps or []), ssid=ssid)


def _rogue_record(t: int, wired: bool = True) -> dict:
    return {"stream": "air_marshal", "bssid": "de:ad:be:ef:00:01",
            "ssid": "centro-ops", "channel": 6, "firstSeen": t - 120_000,
            "lastSeen": t, "wiredMacs": ["00:50:56:aa:bb:cc"] if wired else [],
            "wiredVlans": [12] if wired else [], "manufacturer": "Espressif",
            "encryption": "open", "contained": False,
            "confidence": "observed", "gap": []}


# --------------------------------------------------------------- catalog

def test_catalog_loads_and_every_entry_is_complete():
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    assert doc["meta"]["mode"] == "RECOMMEND_ONLY"
    entries = doc["alerts"]
    assert len(entries) >= 10
    ids = {e["id"] for e in entries}
    assert REQUIRED_IDS <= ids, f"missing: {REQUIRED_IDS - ids}"
    for e in entries:
        missing = REQUIRED_KEYS - set(e)
        assert not missing, f"{e.get('id')} missing keys: {missing}"
        assert e["severity"] in ("info", "warn", "high"), e["id"]
        assert set(e["streams"]) <= VALID_STREAMS, e["id"]
        assert isinstance(e["evidence_required"], list) and e["evidence_required"]
        assert isinstance(e["known_blind_spots"], list) and e["known_blind_spots"], (
            f"{e['id']}: every contextual alert must state what it canNOT know")
        assert isinstance(e["context_conditions"], dict)


def test_recommend_only_no_enforcement_verbs_anywhere():
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    for e in doc["alerts"]:
        assert not IMPERATIVE.search(e["action"]), (
            f"{e['id']} action reads as enforcement: {e['action']!r}")
        assert "recommend" in e["action"].lower(), (
            f"{e['id']} action must be phrased as a recommendation")
    # and the engine's emitted alerts inherit that wording + mode
    eng = AlertEngine(CATALOG)
    an = AttackAnalyzer()
    eng.consume_assessment(an.consume(_rogue_record(10_000)))
    eng.consume_observation(_obs("0c:8b:7d:00:00:01", 1_000,
                                 loc={"x": 5.0, "y": 5.0, "variance": 2.0}))
    eng.consume_observation(_obs("0c:8b:7d:00:00:01", 3_000,
                                 loc={"x": 45.0, "y": 5.0, "variance": 2.0}))
    alerts = eng.evaluate()
    assert alerts
    for a in alerts:
        assert a.mode == "RECOMMEND_ONLY"
        assert not IMPERATIVE.search(a.action), a.alert_id


# ---------------------------------------------------------------- engine

def test_rogue_on_wire_fires_high_with_wire_evidence():
    eng = AlertEngine(CATALOG)
    eng.consume_assessment(AttackAnalyzer().consume(_rogue_record(10_000)))
    alerts = {a.alert_id: a for a in eng.evaluate()}
    a = alerts["rogue_ap_on_wire"]
    assert a.severity == "high"
    assert a.evidence.get("wiredMacs"), (
        "wire evidence is the discriminator and must ride in the alert")
    # VLAN 12 is the POS segment — the escalation variant fires too
    pos = alerts["rogue_ap_on_pos_vlan"]
    assert pos.severity == "high"
    assert pos.evidence["pos_vlans_matched"] == [12]


def test_evil_twin_without_wire_is_warn_and_names_neighbour_blind_spot():
    eng = AlertEngine(CATALOG)
    rec = _rogue_record(10_000, wired=False)
    rec["bssid"] = "aa:bb:cc:00:00:99"
    rec["ssid"] = "centro-ops-guest"   # resembles ops SSID, no wire evidence
    a = AttackAnalyzer().consume(rec)
    assert a.kind == "honeypot_ssid"
    eng.consume_assessment(a)
    (alert,) = [x for x in eng.evaluate() if x.alert_id == "evil_twin_ops_ssid"]
    assert alert.severity == "warn", (
        "no wire evidence: in this building it is probably one of the five "
        "neighbour networks — never high")
    assert alert.confidence == "inferred"
    assert any("neighbour" in b.lower() for b in alert.blind_spots)


def test_deauth_alert_never_claims_intensity():
    eng = AlertEngine(CATALOG)
    a = AttackAnalyzer().consume({
        "stream": "awips_syslog", "signature": "deauth_flood",
        "apMac": "00:2a:10:00:01:00", "t": 5_000,
        "confidence": "observed",
        "gap": ["no_client_identity", "awips_throttled"]})
    eng.consume_assessment(a)
    (alert,) = [x for x in eng.evaluate() if x.alert_id == "deauth_flood"]
    text = (alert.action + " " + " ".join(alert.blind_spots)).lower()
    assert "intensity" in text or "unknown" in text, (
        "throttling destroys intensity — the alert must say so")
    # no count of suppressed frames may be asserted as fact
    assert not any("count" in k.lower() or "frames" in k.lower()
                   for k in alert.evidence), alert.evidence
    assert not re.search(r"\d+\s*(frames|deauth)", alert.action.lower())


def test_containment_active_is_an_operational_risk_alert():
    eng = AlertEngine(CATALOG)
    eng.consume_observation(_obs("a4:5e:60:00:00:01", 7_000, ssid="centro-ops",
                                 gaps=["containment_rtls_degraded"]))
    (alert,) = [x for x in eng.evaluate()
                if x.alert_id == "containment_active_rtls_risk"]
    assert alert.severity == "warn"
    assert "containment_rtls_degraded" in alert.evidence["gap"]


def test_cart_mac_duplicate_fires_on_irreconcilable_fixes_as_inference():
    eng = AlertEngine(CATALOG)
    mac = "0c:8b:7d:00:00:07"           # cart panel vendor OUI
    eng.consume_observation(_obs(mac, 1_000,
                                 loc={"x": 5.0, "y": 5.0, "variance": 2.0}))
    eng.consume_observation(_obs(mac, 3_000,   # 40 m in 2 s — not one cart
                                 loc={"x": 45.0, "y": 5.0, "variance": 2.0}))
    (alert,) = [x for x in eng.evaluate() if x.alert_id == "cart_mac_duplicate"]
    assert alert.confidence == "inferred", (
        "both fixes are cloud inferences; the duplicate is never observed")
    assert alert.evidence["displacement_m"] > 30.0
    assert alert.evidence["dt_s"] < 5.0
    # a plausible cart move (4 m in 2 s across the two windows) must NOT fire
    eng2 = AlertEngine(CATALOG)
    eng2.consume_observation(_obs(mac, 1_000,
                                  loc={"x": 5.0, "y": 5.0, "variance": 2.0}))
    eng2.consume_observation(_obs(mac, 3_000,
                                  loc={"x": 9.0, "y": 5.0, "variance": 2.0}))
    assert not [x for x in eng2.evaluate()
                if x.alert_id == "cart_mac_duplicate"]


def test_probe_surge_counts_are_a_lower_bound_inference():
    eng = AlertEngine(CATALOG)
    for i in range(45):
        eng.consume_observation(_obs(f"ca:fe:00:00:00:{i:02x}",
                                     1_000 + i * 100, ssid=None))
    (alert,) = [x for x in eng.evaluate()
                if x.alert_id == "probe_surge_unassociated"]
    assert alert.confidence == "inferred"
    assert alert.evidence["distinct_mac_count"] >= 40
    assert any("randomised" in b.lower() or "undercount" in b.lower()
               for b in alert.blind_spots), (
        "dropped randomised MACs make every probe count a lower bound")


def test_sensor_silent_after_configurable_window():
    eng = AlertEngine(CATALOG)
    eng.consume_observation(_obs("aa:00:00:00:00:01", 1_000, ssid="x",
                                 rssi=[("00:2a:10:00:00:05", -55.0)]))
    eng.consume_observation(_obs("aa:00:00:00:00:02", 1_000 + 700_000,
                                 ssid="x", rssi=[("00:2a:10:00:00:06", -55.0)]))
    silent = [x for x in eng.evaluate() if x.alert_id == "sensor_silent"]
    assert [a.subject for a in silent] == ["00:2a:10:00:00:05"], (
        "the sensor that stopped appearing in rssiRecords — and only it")
    assert silent[0].confidence == "inferred", (
        "absence is an inference: jittered POSTs mean silence != failure")
    assert silent[0].evidence["silent_for_ms"] >= 600_000


def test_exit_without_checkout_is_a_passthrough_escalation():
    eng = AlertEngine(CATALOG)
    eng.consume_recommendation(Recommendation(
        t_ms=90_000, rule="exit_without_checkout_presence",
        subject_mac="0c:8b:7d:00:00:03",
        action="recommend attendant check at exit gate", severity="warn",
        evidence={"exit_at": (3.0, 1.5), "checkout_interactions": 0}))
    (alert,) = [x for x in eng.evaluate()
                if x.alert_id == "exit_without_checkout"]
    assert alert.severity == "warn"
    assert alert.confidence == "inferred"
    assert alert.evidence["source_policy_rule"] == "exit_without_checkout_presence"
    assert any("sensing gap" in b.lower() for b in alert.blind_spots)


def test_neighbour_network_is_catalogued_as_info():
    eng = AlertEngine(CATALOG)
    rec = _rogue_record(10_000, wired=False)
    rec["ssid"] = "CafeGreg-WiFi"
    eng.consume_assessment(AttackAnalyzer().consume(rec))
    (alert,) = [x for x in eng.evaluate()
                if x.alert_id == "neighbour_network_catalogued"]
    assert alert.severity == "info"
    assert alert.confidence == "inferred", (
        "'neighbour' is a classification, not a heard fact")


def test_dedup_same_alert_and_subject_once_per_window():
    def rogue(t: int) -> AttackAssessment:
        return AttackAssessment(
            t_ms=t, kind="rogue_ap_on_wire", subject="de:ad:be:ef:00:01",
            severity="high", attack_path=[], action="recommend isolation",
            confidence="observed",
            evidence={"wiredMacs": ["00:50:56:aa:bb:cc"], "wiredVlans": [],
                      "channel": 6})
    eng = AlertEngine(CATALOG)   # meta dedup_window_ms = 300_000
    eng.consume_assessment(rogue(10_000))
    assert len(eng.evaluate()) == 1
    eng.consume_assessment(rogue(20_000))          # inside the window
    assert eng.evaluate() == []
    eng.consume_assessment(rogue(10_000 + 400_000))  # window elapsed
    assert len(eng.evaluate()) == 1


# ---------------------------------------------------- layer separation

def test_alert_engine_imports_nothing_from_world_sensing_eval():
    tree = ast.parse((ROOT / "dlai" / "alerts.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    offenders = [m for m in found
                 if m.split(".")[0] in ("world", "sensing", "eval")]
    assert not offenders, (
        f"alerts.py crosses the layer boundary: {offenders} — the blind "
        "test is meaningless if Layer 3 can see below Layer 2")
