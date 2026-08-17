"""The one rule that governs everything: dlai/ must not import world/.

Layer 1 is the ground truth for evaluating Layer 3. If Layer 3 can see
Layer 1, the blind test is meaningless. Enforced at the AST level so it
cannot rot: every import statement in every module under dlai/ is
checked, including function-local imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_dlai_never_imports_world():
    offenders = []
    for py in (ROOT / "dlai").rglob("*.py"):
        for mod in _imports_of(py):
            if mod == "world" or mod.startswith("world."):
                offenders.append((py.name, mod))
    assert not offenders, (
        f"Layer 3 imports Layer 1: {offenders} — the blind test is now "
        "meaningless; route the data through sensing/ instead")


def test_dlai_never_imports_sensing_internals_or_eval():
    # dlai consumes sensing OUTPUT (json records), not sensing code, and
    # certainly not the evaluation harness that holds truth_links().
    offenders = []
    for py in (ROOT / "dlai").rglob("*.py"):
        for mod in _imports_of(py):
            if mod.split(".")[0] in ("sensing", "eval"):
                offenders.append((py.name, mod))
    assert not offenders, f"Layer 3 imports beyond its charter: {offenders}"


def test_world_never_imports_sensing_or_dlai():
    # Layer 1 must be describable without mentioning a single sensor.
    offenders = []
    for py in (ROOT / "world").rglob("*.py"):
        for mod in _imports_of(py):
            if mod.split(".")[0] in ("sensing", "dlai"):
                offenders.append((py.name, mod))
    assert not offenders, f"Layer 1 mentions sensors: {offenders}"


def test_attack_shapes_module_is_pure_layer2():
    # viz/attack_shapes.py builds Layer-2 observation dicts and nothing more.
    # It must import no world/, sensing/, or dlai/ code, so the physical-twin
    # producer can emit injected attacks without touching any Layer-3 module.
    offenders = [
        mod for mod in _imports_of(ROOT / "viz" / "attack_shapes.py")
        if mod.split(".")[0] in ("world", "sensing", "dlai")]
    assert not offenders, f"attack_shapes reaches beyond Layer 2: {offenders}"


def test_twin_producer_never_pulls_dlai_at_runtime():
    # The two-program split: the twin (viz/server.py) is a Layer-1/2 producer
    # that only EMITS observations. It must not transitively import dlai/, so
    # the DLAI console can live in a separate repo the twin has no code from.
    import importlib
    import sys

    for name in [m for m in list(sys.modules) if m.split(".")[0] == "dlai"]:
        del sys.modules[name]
    importlib.import_module("viz.server")
    leaked = sorted(m for m in sys.modules if m.split(".")[0] == "dlai")
    assert not leaked, f"twin producer pulled Layer-3 modules: {leaked}"


def test_identity_trap_cart_entity_has_no_mac_field():
    # In Layer 1 a cart has cart_id — a fact. A MAC is an observation.
    # If they sit in the same record the separation has collapsed.
    src = (ROOT / "world" / "entities.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Cart":
            fields = [n.target.id for n in node.body
                      if isinstance(n, ast.AnnAssign)]
            assert not any("mac" in f.lower() for f in fields), (
                "Cart carries a MAC — the identity trap has sprung")
