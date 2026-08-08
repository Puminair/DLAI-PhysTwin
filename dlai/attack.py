"""Layer 3 — attack path assessment and decision. RECOMMEND_ONLY.

May import: stdlib, dlai.*. NEVER world/.

Consumes Air Marshal and aWIPS records routed by dlai.ingest. The
discriminating field is `wiredMacs`: a BSSID seen on our own wire is a
rogue ON the network; one without is — in this building — most likely
one of the five neighbour networks on the same floor or above. aWIPS
syslog carries no client identity and is throttled, so intensity is
explicitly unknowable, and the assessment says so instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODE = "RECOMMEND_ONLY"


@dataclass(frozen=True)
class AttackAssessment:
    t_ms: int
    kind: str
    subject: str
    severity: str            # "info" | "warn" | "high"
    attack_path: list[str]
    action: str
    confidence: str          # "observed" | "inferred"
    evidence: dict = field(default_factory=dict)
    mode: str = MODE


class AttackAnalyzer:
    def __init__(self, own_ssids: tuple[str, ...] = ("centro-ops",)):
        self.own_ssids = own_ssids
        self.assessments: list[AttackAssessment] = []

    def consume(self, record: dict) -> AttackAssessment | None:
        stream = record.get("stream")
        if stream == "air_marshal":
            a = self._air_marshal(record)
        elif stream == "awips_syslog":
            a = self._awips(record)
        else:
            return None
        if a is not None:
            self.assessments.append(a)
        return a

    def _air_marshal(self, r: dict) -> AttackAssessment | None:
        wired = r.get("wiredMacs") or []
        ssid = r.get("ssid", "")
        if wired:
            return AttackAssessment(
                t_ms=r["lastSeen"], kind="rogue_ap_on_wire",
                subject=r["bssid"], severity="high",
                attack_path=["rogue AP bridged to store VLAN",
                             "L2 access to POS/ops segment",
                             "lateral movement to payment infrastructure"],
                action="recommend isolating the switch port carrying "
                       f"wiredMacs={wired} and dispatching to locate the device",
                confidence="observed",
                evidence={"wiredMacs": wired, "wiredVlans": r.get("wiredVlans"),
                          "channel": r.get("channel")})
        if ssid in self.own_ssids or _looks_like(ssid, self.own_ssids):
            return AttackAssessment(
                t_ms=r["lastSeen"], kind="honeypot_ssid",
                subject=r["bssid"], severity="warn",
                attack_path=["evil-twin SSID lure", "client credential capture"],
                action="recommend on-air verification before any containment "
                       "(containment time-splits the scanning radio and "
                       "degrades RTLS accuracy)",
                confidence="inferred",
                evidence={"ssid": ssid, "note": "no wire evidence; could be "
                          "neighbour — 5 neighbour networks share this floor"})
        return AttackAssessment(
            t_ms=r["lastSeen"], kind="neighbour_network",
            subject=r["bssid"], severity="info",
            attack_path=[],
            action="no action; catalogue as neighbour",
            confidence="inferred",
            evidence={"ssid": ssid, "wiredMacs": []})

    def _awips(self, r: dict) -> AttackAssessment:
        return AttackAssessment(
            t_ms=r["t"], kind=f"awips_{r['signature']}",
            subject=r["apMac"], severity="warn",
            attack_path=["over-the-air denial of service"],
            action="recommend correlating across APs by time bucket; "
                   "intensity is NOT recoverable from this stream",
            confidence="observed",
            evidence={"note": "aWIPS carries no client identity; throttled to "
                              "one message per signature per AP per interval — "
                              "attack intensity is lost",
                      "gap": r.get("gap", [])})


def _looks_like(ssid: str, own: tuple[str, ...]) -> bool:
    s = ssid.lower().replace(" ", "").replace("-", "").replace("_", "")
    return any(o.lower().replace("-", "") in s and ssid != o for o in own)
