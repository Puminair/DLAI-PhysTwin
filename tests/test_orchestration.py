"""Layer 3 incident orchestration: correlate alerts → kill-chain → playbook."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from dlai.orchestration import IncidentOrchestrator

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeAlert:
    alert_id: str
    severity: str
    subject: str
    t_ms: int = 1000
    action: str = ""
    confidence: str = "observed"
    mode: str = "RECOMMEND_ONLY"
    evidence: dict = None
    blind_spots: list = None


def test_orchestration_imports_no_forbidden_layer():
    tree = ast.parse((ROOT / "dlai" / "orchestration.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
    assert not any(m.split(".")[0] in ("world", "sensing", "eval") for m in mods)


def test_two_rogue_alerts_correlate_into_one_payment_incident():
    orc = IncidentOrchestrator()
    orc.consume(FakeAlert("rogue_ap_on_wire", "high", "de:ad:be:ef:00:02", 1000))
    orc.consume(FakeAlert("rogue_ap_on_pos_vlan", "high", "de:ad:be:ef:00:02", 1100))
    incs = orc.incidents()
    assert len(incs) == 1, "the two rogue alerts must fold into one incident"
    inc = incs[0]
    assert inc["priority"] == "P1", "POS involvement escalates to P1"
    assert "Payment" in inc["title"]
    assert set(inc["alert_ids"]) == {"rogue_ap_on_wire", "rogue_ap_on_pos_vlan"}


def test_kill_chain_is_ordered_and_reflects_the_alerts():
    orc = IncidentOrchestrator()
    orc.consume(FakeAlert("rogue_ap_on_wire", "high", "x", 1))
    orc.consume(FakeAlert("rogue_ap_on_pos_vlan", "high", "x", 2))
    chain = [k["stage"] for k in orc.incidents()[0]["kill_chain"]]
    assert chain == ["initial-access", "lateral-movement"], chain


def test_every_incident_has_a_recommend_only_playbook():
    orc = IncidentOrchestrator()
    for aid, sev in [("evil_twin_ops_ssid", "warn"), ("deauth_flood", "warn"),
                     ("cart_mac_duplicate", "high"), ("sensor_silent", "warn")]:
        orc.consume(FakeAlert(aid, sev, "s-" + aid, 1))
    for inc in orc.incidents():
        assert inc["mode"] == "RECOMMEND_ONLY"
        assert inc["playbook"], f"{inc['type']} has no playbook"
        for step in inc["playbook"]:
            assert {"phase", "action", "rationale"} <= step.keys()
        # no step may read as automated enforcement
        text = " ".join(s["action"].lower() for s in inc["playbook"])
        assert "auto-disable" not in text or "do not auto-disable" in text
        assert "automatically block" not in text


def test_priority_rolls_up_to_the_worst_alert():
    orc = IncidentOrchestrator()
    orc.consume(FakeAlert("cart_impossible_motion", "warn", "m", 1))
    orc.consume(FakeAlert("cart_mac_duplicate", "high", "m", 2))
    assert orc.incidents()[0]["priority"] == "P2"   # high → P2 (no POS)


def test_distinct_campaigns_stay_separate():
    orc = IncidentOrchestrator()
    orc.consume(FakeAlert("rogue_ap_on_wire", "high", "a", 1))
    orc.consume(FakeAlert("deauth_flood", "warn", "b", 2))
    types = {i["type"] for i in orc.incidents()}
    assert {"network_intrusion", "availability_attack"} <= types
