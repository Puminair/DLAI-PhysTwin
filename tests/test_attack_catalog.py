"""The store-specific attack catalogue: coherent, cross-referenced, defensive.

Every attack must map to a real detection or an honest blind spot; the
catalogue is the offensive mirror of config/alerts.yaml and must not
drift from it.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    with open(ROOT / "config" / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_every_attack_has_the_required_fields():
    cat = _load("attack_catalog.yaml")
    valid_cats = set(cat["categories"])
    valid_sev = {"info", "warn", "serious", "critical"}
    for a in cat["attacks"]:
        for key in ("id", "name", "category", "target", "vector",
                    "store_context", "severity", "detected_by", "blind_spot",
                    "mitigation"):
            assert key in a, f"{a.get('id','?')} missing {key}"
        assert a["category"] in valid_cats, f"{a['id']} bad category"
        assert a["severity"] in valid_sev, f"{a['id']} bad severity {a['severity']}"
        assert isinstance(a["detected_by"], list)
        assert isinstance(a["blind_spot"], bool)


def test_attack_ids_are_unique():
    ids = [a["id"] for a in _load("attack_catalog.yaml")["attacks"]]
    assert len(ids) == len(set(ids)), "duplicate attack id"


def test_every_detection_references_a_real_alert():
    alerts = _load("alerts.yaml")
    alert_ids = {a["id"] for a in alerts["alerts"]}
    for a in _load("attack_catalog.yaml")["attacks"]:
        for det in a["detected_by"]:
            assert det in alert_ids, (
                f"attack {a['id']} claims detection by unknown alert {det!r}")


def test_blind_spots_are_honest():
    # a blind spot must say why, and must not claim a clean detection
    for a in _load("attack_catalog.yaml")["attacks"]:
        if a["blind_spot"]:
            assert a.get("blind_spot_reason"), f"{a['id']} blind without a reason"
        else:
            # a non-blind attack should actually be detectable
            assert a["detected_by"], f"{a['id']} is neither detected nor blind"


def test_catalogue_covers_the_documented_blind_spots():
    # the structural limits CLAUDE.md §5/§9 names must each appear as a
    # blind-spot attack, or the threat model is lying by omission
    blind = {a["id"] for a in _load("attack_catalog.yaml")["attacks"]
             if a["blind_spot"]}
    for needed in ("perimeter_inside_outside_evasion", "awips_throttle_blinding",
                   "mac_randomization_evasion", "station_to_station_covert",
                   "cold_room_rf_shadow"):
        assert needed in blind, f"documented blind spot {needed} not catalogued"


def test_posture_is_recommend_only():
    cat = _load("attack_catalog.yaml")
    assert cat["meta"]["posture"] == "RECOMMEND_ONLY"
    # no mitigation should read as an automated enforcement imperative
    import re
    bad = re.compile(r"\b(auto-?block|automatically block|auto-?disconnect)\b", re.I)
    for a in cat["attacks"]:
        assert not bad.search(a["mitigation"]), f"{a['id']} implies auto-enforcement"
