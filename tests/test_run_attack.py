"""The attack runner: catalogue attacks drive DLAI end to end."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_attack", ROOT / "scripts" / "run_attack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load()
cfg = __import__("config").load_config()


def test_every_handler_id_is_a_real_attack():
    ids = {a["id"] for a in ra.load_catalog()["attacks"]}
    assert set(ra.HANDLERS) <= ids, "runner wires an attack not in the catalogue"


def test_rogue_on_wire_fires_high_recommend_only():
    alerts = ra.atk_rogue_on_wire(cfg)
    assert alerts, "rogue-on-wire produced no alert"
    a = alerts[0]
    assert a.alert_id == "rogue_ap_on_wire"
    assert a.severity == "high"
    assert a.mode == "RECOMMEND_ONLY"
    assert a.confidence == "observed"


def test_evil_twin_caps_at_warn_without_wire_evidence():
    alerts = ra.atk_evil_twin(cfg)
    assert alerts
    assert alerts[0].severity == "warn", "evil twin without wire must not be high"


def test_cart_mac_clone_runs_the_physical_twin_and_fires():
    # this genuinely runs WorldSim + real sensing + DLAI
    alerts = ra.atk_cart_mac_clone(cfg)
    assert alerts, "cloning a real panel produced no alert"
    a = alerts[0]
    assert a.alert_id == "cart_mac_duplicate"
    assert a.confidence == "inferred", "a cloned position is inferred, not observed"


def test_deauth_never_claims_intensity():
    alerts = ra.atk_deauth(cfg)
    assert alerts
    text = (alerts[0].action + " " + " ".join(alerts[0].blind_spots)).lower()
    assert "intensity" in text or "unknow" in text


def test_blind_spot_ids_have_no_handler_but_carry_a_reason():
    for a in ra.load_catalog()["attacks"]:
        if a["blind_spot"]:
            assert a["id"] not in ra.HANDLERS
            assert a.get("blind_spot_reason")


def test_list_runs_without_error(capsys):
    ra.main(["--list"])
    out = capsys.readouterr().out
    assert "rogue_ap_on_wire" in out
    assert "blind spot" in out
