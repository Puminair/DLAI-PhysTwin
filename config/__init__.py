"""Configuration — the only home for `assumed` constants.

Layer: none (shared by all layers; imports nothing from world/, sensing/
or dlai/). Every value marked `assumed` in defaults.yaml must be read
through here so it stays overridable at runtime.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

import yaml

_DEFAULTS_PATH = Path(__file__).parent / "defaults.yaml"


class Config:
    """Dot-path access over the merged config mapping.

    ``cfg.get("sensors.mount_z_m")`` -> 3.0.  Raises KeyError on unknown
    paths so a typo never silently returns a default.
    """

    def __init__(self, data: Mapping[str, Any]):
        self._data = dict(data)

    def get(self, path: str) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(f"unknown config path: {path!r} (failed at {part!r})")
            node = node[part]
        return node

    def section(self, path: str) -> dict:
        node = self.get(path)
        if not isinstance(node, Mapping):
            raise KeyError(f"config path {path!r} is not a section")
        return copy.deepcopy(dict(node))

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)


def _deep_merge(base: dict, override: Mapping) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(overrides: Mapping[str, Any] | None = None,
                overrides_path: str | Path | None = None) -> Config:
    """Load defaults.yaml, then apply optional runtime overrides.

    ``overrides`` is a nested mapping merged over the defaults;
    ``overrides_path`` points at a YAML file merged the same way.
    This is the runtime-override mechanism CLAUDE.md requires for every
    `assumed` constant.
    """
    with open(_DEFAULTS_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if overrides_path is not None:
        with open(overrides_path, "r", encoding="utf-8") as fh:
            file_overrides = yaml.safe_load(fh) or {}
        data = _deep_merge(data, file_overrides)
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(data)
