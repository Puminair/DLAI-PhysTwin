"""Layer 3 — incident orchestration over the alert stream.

May import: stdlib, dlai.*. NEVER world/, sensing/, eval/.

The alert engine says *what* fired. Orchestration answers *so what, and
what next*: it correlates related alerts into a single incident, places
each on a defensive kill-chain, rolls the incident up to a priority, and
emits an ordered RECOMMEND_ONLY response playbook. It runs entirely off
the alerts — no new observation, no ground truth.

RECOMMEND_ONLY is absolute here too: a playbook is a sequence of
*recommendations* a human executes, never an action the system takes. No
step auto-isolates, auto-contains, or auto-disconnects; containment steps
are explicitly gated on human confirmation and on the RTLS-degradation
cost that containment carries in this building.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODE = "RECOMMEND_ONLY"

# each alert's place on a defensive kill-chain (MITRE-flavoured, plain words)
STAGE = {
    "neighbour_network_catalogued": "reconnaissance",
    "probe_surge_unassociated": "reconnaissance",
    "evil_twin_ops_ssid": "initial-access",
    "rogue_ap_on_wire": "initial-access",
    "supply_chain_dock_rogue": "initial-access",
    "rogue_ap_on_pos_vlan": "lateral-movement",
    "sensor_silent": "defense-evasion",
    "deauth_flood": "impact",
    "containment_active_rtls_risk": "impact",
    "cart_mac_duplicate": "impact",
    "cart_impossible_motion": "impact",
    "exit_without_checkout": "impact",
}
STAGE_ORDER = ["reconnaissance", "initial-access", "defense-evasion",
               "lateral-movement", "impact"]

# alert -> incident type (the campaign it belongs to)
_TYPE = {
    "rogue_ap_on_wire": "network_intrusion",
    "rogue_ap_on_pos_vlan": "network_intrusion",
    "supply_chain_dock_rogue": "network_intrusion",
    "evil_twin_ops_ssid": "credential_harvest",
    "probe_surge_unassociated": "credential_harvest",
    "deauth_flood": "availability_attack",
    "containment_active_rtls_risk": "availability_attack",
    "cart_mac_duplicate": "asset_integrity",
    "cart_impossible_motion": "asset_integrity",
    "exit_without_checkout": "asset_integrity",
    "sensor_silent": "sensing_tamper",
}

_SEV_RANK = {"info": 1, "warn": 2, "high": 3, "critical": 4}
_RANK_PRIORITY = {4: "P1", 3: "P2", 2: "P3", 1: "P4", 0: "P4"}


def _step(phase: str, action: str, rationale: str) -> dict:
    return {"phase": phase, "action": action, "rationale": rationale}


# ordered RECOMMEND_ONLY playbooks, one per incident type
PLAYBOOKS = {
    "network_intrusion": [
        _step("contain", "Recommend isolating the switch port carrying the "
              "reported wiredMacs — dispatch to confirm first; do not auto-disable.",
              "wiredMacs is the only field separating this from a neighbour."),
        _step("escalate", "Recommend notifying the security lead; if the POS "
              "VLAN is implicated, open a PCI incident review.",
              "L2 attachment to the payment segment is a card-data risk."),
        _step("investigate", "Recommend dispatching staff to physically locate "
              "the device — RF cannot place it, inside or outside.",
              "Perimeter units do not resolve inside/outside here."),
        _step("evidence", "Recommend preserving the switch and AP logs and the "
              "Air Marshal record before any port change.",
              "The wire evidence is the case; keep it."),
        _step("verify", "Recommend verifying POS terminal integrity before "
              "closing the incident.",
              "Wire attachment proves adjacency, not what already crossed it."),
    ],
    "credential_harvest": [
        _step("verify", "Recommend on-air verification against the neighbour "
              "catalogue BEFORE any containment.",
              "Five neighbour networks share this floor; this may be one."),
        _step("contain", "If confirmed hostile, recommend targeted containment "
              "in a low-traffic window only.",
              "Containment time-splits the scanning radio and degrades RTLS."),
        _step("notify", "Recommend advising staff to distrust the ops-SSID "
              "prompt and rotating credentials if capture is suspected.",
              "Evil-twin's objective is credential capture."),
        _step("monitor", "Recommend watching for a correlated new BSSID.",
              "Probe counts are lower bounds — randomised MACs never reach us."),
    ],
    "availability_attack": [
        _step("correlate", "Recommend correlating deauth reports across APs by "
              "time bucket to bound the affected area.",
              "aWIPS throttling destroys intensity — do not estimate it."),
        _step("protect", "Recommend prioritising the ops/POS SSIDs; note RTLS "
              "is degraded while containment runs.",
              "Availability of carts/POS is the target."),
        _step("dispatch", "Recommend dispatching to the bounded area to locate "
              "the source.",
              "The stream carries no client identity or position."),
        _step("verify", "Recommend confirming client connectivity recovered "
              "before closing.",
              "Impact is measured on the clients, not the syslog."),
    ],
    "asset_integrity": [
        _step("inspect", "Recommend physical inspection of the cart carrying "
              "the panel.",
              "One MAC in two places is physically impossible."),
        _step("audit", "Recommend auditing the panel fleet MAC registry and the "
              "dock inventory for a cloned panel.",
              "Cloning or a gross RTLS error both present this way."),
        _step("corroborate", "Recommend corroborating with POS and CCTV — the "
              "RTLS position is inferred and low-trust.",
              "Both sightings are inferred, not observed."),
        _step("verify", "Recommend reconciling the duplicate before closing.",
              "Do not act on a single pair of fixes."),
    ],
    "sensing_tamper": [
        _step("check", "Recommend checking the sensor's PoE budget and switch "
              "port (802.3at).",
              "A silent unit may be starved, not attacked."),
        _step("dispatch", "Recommend dispatching to inspect the unit and its "
              "mount physically.",
              "Silence has many causes; confirm on site."),
        _step("compensate", "Recommend noting the coverage gap — nearby cells "
              "may drop below positionable until restored.",
              "Losing one pair costs +2.5 dB diversity near the 3-AP edge."),
    ],
    "monitoring": [
        _step("catalogue", "Recommend cataloguing and continuing to monitor; no "
              "action.",
              "Informational — a neighbour or a low-signal event."),
    ],
}

_TITLES = {
    "network_intrusion": "Wired intrusion",
    "credential_harvest": "Credential-harvest attempt",
    "availability_attack": "Over-the-air availability attack",
    "asset_integrity": "Cart-identity integrity",
    "sensing_tamper": "Sensor tamper / outage",
    "monitoring": "Monitoring",
}


@dataclass
class Incident:
    incident_id: str
    type: str
    title: str
    priority: str = "P4"
    subjects: set = field(default_factory=set)
    alert_ids: list = field(default_factory=list)
    stages: dict = field(default_factory=dict)      # stage -> [alert_id...]
    first_ms: int = 0
    last_ms: int = 0
    _rank: int = 0

    def kill_chain(self) -> list:
        return [{"stage": s, "alerts": self.stages[s]}
                for s in STAGE_ORDER if s in self.stages]

    def playbook(self) -> list:
        steps = PLAYBOOKS.get(self.type, PLAYBOOKS["monitoring"])
        return [dict(s) for s in steps]

    def as_dict(self) -> dict:
        return {"incident_id": self.incident_id, "type": self.type,
                "title": self.title, "priority": self.priority,
                "subjects": sorted(self.subjects), "alert_ids": self.alert_ids,
                "kill_chain": self.kill_chain(), "playbook": self.playbook(),
                "mode": MODE, "first_ms": self.first_ms, "last_ms": self.last_ms}


class IncidentOrchestrator:
    """Correlates alerts into prioritised incidents with response playbooks."""

    def __init__(self):
        self._incidents: dict[str, Incident] = {}   # keyed by incident type
        self._seq = 0

    def consume(self, alert) -> Incident:
        """Fold one Alert into its incident; return that incident."""
        itype = _TYPE.get(alert.alert_id, "monitoring")
        inc = self._incidents.get(itype)
        if inc is None:
            self._seq += 1
            inc = Incident(incident_id=f"INC-{self._seq:03d}", type=itype,
                           title=_TITLES[itype], first_ms=alert.t_ms)
            self._incidents[itype] = inc

        inc.alert_ids.append(alert.alert_id)
        inc.subjects.add(alert.subject)
        inc.last_ms = max(inc.last_ms, alert.t_ms)
        stage = STAGE.get(alert.alert_id, "impact")
        inc.stages.setdefault(stage, [])
        if alert.alert_id not in inc.stages[stage]:
            inc.stages[stage].append(alert.alert_id)

        # priority rollup: max severity, bumped to P1 if payment is implicated
        inc._rank = max(inc._rank, _SEV_RANK.get(alert.severity, 1))
        inc.priority = _RANK_PRIORITY[inc._rank]
        if "rogue_ap_on_pos_vlan" in inc.alert_ids:
            inc.priority = "P1"
            inc.title = "Payment-infrastructure intrusion"
        return inc

    def incidents(self) -> list[dict]:
        """Open incidents, highest priority first."""
        order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
        return [i.as_dict() for i in sorted(
            self._incidents.values(),
            key=lambda i: (order.get(i.priority, 9), -i.last_ms))]
