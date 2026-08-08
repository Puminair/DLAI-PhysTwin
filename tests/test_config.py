"""Config: assumed constants are overridable at runtime, typos fail loud."""
from __future__ import annotations

import pytest

from config import load_config


def test_defaults_load_and_verified_constants_hold():
    cfg = load_config()
    assert cfg.get("areas.sales_floor_m2") == 2782
    assert cfg.get("geometry.columns") == 77
    assert cfg.get("rules.location_threshold_dbm") == -67
    # band keys contain a dot — access them as a section, not a dot-path
    assert cfg.get("attenuation_db")["2.4"]["gondola"] == 17.7


def test_assumed_constants_are_runtime_overridable():
    cfg = load_config(overrides={"attenuation_db": {"2.4": {"gondola": 21.0}}})
    assert cfg.get("attenuation_db")["2.4"]["gondola"] == 21.0
    # untouched siblings survive the merge
    assert cfg.get("attenuation_db")["2.4"]["column"] == 46.4
    assert cfg.get("heights.cart_panel_z_m") == 1.00


def test_unknown_path_raises_instead_of_defaulting():
    with pytest.raises(KeyError):
        load_config().get("sensors.mount_z_typo")


def test_cold_room_is_opaque_null():
    cfg = load_config()
    assert cfg.get("attenuation_db")["2.4"]["cold_room"] is None
