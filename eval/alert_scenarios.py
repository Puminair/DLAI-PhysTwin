"""Evaluation — three scripted 'circulations' that exercise the whole
contextual alert catalog end to end.

eval/ may import dlai/. It builds the engine's inputs only from dlai
types (AttackAssessment via dlai.attack.AttackAnalyzer, Recommendation)
and plain dicts — never from world/ or sensing/, so nothing here can leak
Layer-1 truth or Layer-2 internals into the Layer-3 decision.

Each circulation is deterministic: no RNG, no wall clock. Every
timestamp is passed in, so a run is byte-reproducible. Across the three,
every alert id in config/alerts.yaml fires at least once — that is the
'all possible alert types' guarantee, pinned by run_all_circulations().

Everything is RECOMMEND_ONLY: the circulations drive the engine, they
never act.
"""
from __future__ import annotations

from dlai.alerts import Alert, AlertEngine
from dlai.attack import AttackAnalyzer
from dlai.ingest import NormalisedObservation
from dlai.policy import Recommendation

# cart panel vendor OUI (config/alerts.yaml cart_oui_prefixes; matches
# sensing.pipeline panel MAC scheme "0c:8b:7d"). Panels are stable on 2.4.
CART_OUI = "0c:8b:7d"


def _obs(mac: str, t_ms: int, *, loc: dict | None = None,
         rssi: list[tuple[str, float]] | None = None,
         ssid: str | None = None,
         gaps: list[str] | None = None) -> NormalisedObservation:
    """A normalised Scanning-API observation — the shape dlai.ingest emits."""
    return NormalisedObservation(
        mac=mac, seen_time_ms=t_ms, delivered_at_ms=t_ms,
        rssi_dbm=list(rssi or []), cloud_location=loc,
        gaps=list(gaps or []), ssid=ssid)


def _air_marshal(*, bssid: str, ssid: str, t_ms: int,
                 wired_macs: list[str], wired_vlans: list[int],
                 channel: int = 6, encryption: str = "wpa2") -> dict:
    """Air Marshal record shape (Layer-2 output, built here as a plain dict)."""
    return {
        "stream": "air_marshal", "bssid": bssid, "ssid": ssid,
        "channel": channel, "firstSeen": t_ms - 120_000, "lastSeen": t_ms,
        "wiredMacs": list(wired_macs), "wiredVlans": list(wired_vlans),
        "manufacturer": "Various", "encryption": encryption,
        "contained": False, "confidence": "observed", "gap": []}


def _awips(*, signature: str, ap_mac: str, t_ms: int) -> dict:
    """aWIPS syslog record — no client identity, throttled (intensity lost)."""
    return {"stream": "awips_syslog", "signature": signature, "apMac": ap_mac,
            "t": t_ms, "confidence": "observed",
            "gap": ["no_client_identity", "awips_throttled"]}


# --------------------------------------------------------------------------
# Circulation 1 — the Air Marshal wire-vs-air story.
# --------------------------------------------------------------------------
def circulation_wire_threat(t0_ms: int = 1_000_000) -> list[Alert]:
    """Fires: rogue_ap_on_wire, rogue_ap_on_pos_vlan, evil_twin_ops_ssid,
    neighbour_network_catalogued.

    The whole story is `wiredMacs`: with it, a BSSID is a rogue ON the
    network; without it, in this building it is almost certainly one of
    the five neighbour networks on this floor or above.
    """
    engine = AlertEngine()
    analyzer = AttackAnalyzer()          # own SSID default: ("centro-ops",)

    # Five neighbour networks — no wire evidence -> catalogued, info.
    neighbours = ["HaMashbir-Guest", "CafeGreg-WiFi", "Terminal-X-POS",
                  "SuperPharm-Ops", "Centro-Facilities"]
    for i, ssid in enumerate(neighbours):
        rec = _air_marshal(bssid=f"b8:27:eb:00:00:{i:02x}", ssid=ssid,
                           t_ms=t0_ms, wired_macs=[], wired_vlans=[])
        engine.consume_assessment(analyzer.consume(rec))

    # An SSID that resembles the ops network, seen on air, NO wire evidence
    # -> evil twin candidate, warn (never high: could be a neighbour).
    twin = _air_marshal(bssid="aa:bb:cc:00:00:99", ssid="centro-ops-guest",
                        t_ms=t0_ms + 1_000, wired_macs=[], wired_vlans=[],
                        encryption="open")
    engine.consume_assessment(analyzer.consume(twin))

    # A rogue AP bridged onto the POS VLAN (12) -> rogue_ap_on_wire (high)
    # AND the POS-VLAN escalation variant (high).
    rogue = _air_marshal(bssid="de:ad:be:ef:00:01", ssid="centro-ops",
                         t_ms=t0_ms + 2_000,
                         wired_macs=["00:50:56:aa:bb:cc"], wired_vlans=[12],
                         encryption="open")
    engine.consume_assessment(analyzer.consume(rogue))

    return engine.evaluate()


# --------------------------------------------------------------------------
# Circulation 2 — the over-the-air / operational-risk story.
# --------------------------------------------------------------------------
def circulation_rf_attack(t0_ms: int = 2_000_000) -> list[Alert]:
    """Fires: deauth_flood, containment_active_rtls_risk,
    probe_surge_unassociated, sensor_silent.

    aWIPS destroys intensity; containment degrades RTLS for the whole
    floor; a probe surge is only ever a lower bound; a sensor that stops
    appearing in rssiRecords may be down or may just be a delivery gap.
    """
    engine = AlertEngine()
    analyzer = AttackAnalyzer()

    silent_ap = "00:2a:10:00:00:05"
    live_ap = "00:2a:10:00:00:06"

    # A sensor heard early, then never again — used for sensor_silent.
    engine.consume_observation(_obs("a4:5e:60:00:00:01", t0_ms, ssid="centro-ops",
                                    rssi=[(silent_ap, -55.0)]))

    # Deauth flood reported by aWIPS -> deauth_flood (intensity unknown).
    engine.consume_assessment(analyzer.consume(
        _awips(signature="deauth_flood", ap_mac=live_ap, t_ms=t0_ms + 1_000)))

    # Containment activated on a suspected rogue: the scanning radio
    # time-splits and RTLS degrades floor-wide -> operational-risk alert.
    engine.consume_observation(_obs("a4:5e:60:00:00:02", t0_ms + 2_000,
                                    ssid="centro-ops",
                                    gaps=["containment_rtls_degraded"]))

    # A surge of distinct unassociated devices probing (ssid=None).
    for i in range(45):
        engine.consume_observation(
            _obs(f"ca:fe:00:00:{i // 256:02x}:{i % 256:02x}",
                 t0_ms + 3_000 + i * 100, ssid=None))

    # Clock advances past the silent window (600 s) with the live AP still
    # reporting but the early AP gone silent -> sensor_silent (inferred).
    engine.consume_observation(_obs("a4:5e:60:00:00:03", t0_ms + 700_000,
                                    ssid="centro-ops",
                                    rssi=[(live_ap, -58.0)]))

    return engine.evaluate()


# --------------------------------------------------------------------------
# Circulation 3 — the cart-identity / loss-prevention story.
# --------------------------------------------------------------------------
def circulation_identity_and_shrink(t0_ms: int = 3_000_000) -> list[Alert]:
    """Fires: cart_mac_duplicate, cart_impossible_motion,
    exit_without_checkout.

    A stable cart MAC is an identity anchor — until it appears twice at
    once (clone) or moves faster than a cart can (spoof). Every position
    here is an inferred cloud fix, never an observation.
    """
    engine = AlertEngine()

    # (a) Duplicate: the same panel MAC at two irreconcilable positions
    # 40 m apart within 2 s -> cannot be one physical cart.
    dup_mac = f"{CART_OUI}:00:00:07"
    engine.consume_observation(_obs(dup_mac, t0_ms,
                                    loc={"x": 5.0, "y": 5.0, "variance": 2.0}))
    engine.consume_observation(_obs(dup_mac, t0_ms + 2_000,
                                    loc={"x": 45.0, "y": 5.0, "variance": 2.0}))

    # (b) Impossible motion: a different panel making sustained ~3.3 m/s
    # steps (10 m every 3 s) — over the 1.4 m/s cap, but each step < 30 m
    # so it is not a duplicate. Needs a run of over-speed segments.
    mot_mac = f"{CART_OUI}:00:00:11"
    for k in range(5):
        engine.consume_observation(_obs(
            mot_mac, t0_ms + 10_000 + k * 3_000,
            loc={"x": 8.0 + 10.0 * k, "y": 20.0, "variance": 2.0}))

    # (c) Exit without checkout presence: a pass-through escalation of the
    # policy engine's recommendation (dlai.policy).
    engine.consume_recommendation(Recommendation(
        t_ms=t0_ms + 40_000, rule="exit_without_checkout_presence",
        subject_mac=f"{CART_OUI}:00:00:22",
        action="recommend attendant check at exit gate", severity="warn",
        evidence={"exit_at": (3.0, 1.5), "checkout_interactions": 0,
                  "note": "position is inferred; absence of checkout presence "
                          "may be a sensing gap, not theft"}))

    return engine.evaluate()


CIRCULATIONS = (
    circulation_wire_threat,
    circulation_rf_attack,
    circulation_identity_and_shrink,
)


def run_all_circulations() -> dict[str, list[Alert]]:
    """Run all three, assert their union covers the FULL catalog, return them.

    If the catalog grows and no circulation exercises the new alert, the
    union assertion fails — the 'all possible alert types' guarantee.
    """
    results = {fn.__name__: fn() for fn in CIRCULATIONS}

    fired: set[str] = set()
    for alerts in results.values():
        for a in alerts:
            assert a.mode == "RECOMMEND_ONLY", a.alert_id
            fired.add(a.alert_id)

    catalog_ids = set(AlertEngine().catalog)
    missing = catalog_ids - fired
    extra = fired - catalog_ids
    assert not missing, f"catalog alerts never exercised: {sorted(missing)}"
    assert not extra, f"engine fired unknown alert ids: {sorted(extra)}"
    return results


def _report(results: dict[str, list[Alert]]) -> str:
    lines: list[str] = []
    for name, alerts in results.items():
        lines.append(f"\n=== {name} — {len(alerts)} alert(s) ===")
        for a in sorted(alerts, key=lambda x: (x.alert_id, x.subject)):
            action = " ".join(a.action.split())
            if len(action) > 80:
                action = action[:77] + "..."
            lines.append(
                f"  [{a.severity:>4}] {a.alert_id:<28} {a.subject:<22} "
                f"({a.confidence}) {action}")
    fired = {a.alert_id for al in results.values() for a in al}
    lines.append(f"\nUnion fired {len(fired)} of "
                 f"{len(AlertEngine().catalog)} catalog alert ids — "
                 f"{'FULL COVERAGE' if fired == set(AlertEngine().catalog) else 'GAP'}.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(_report(run_all_circulations()))
