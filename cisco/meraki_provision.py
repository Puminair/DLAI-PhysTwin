"""Meraki Dashboard API v1 provisioning for the 150 CW9172I positions.

Layer: none — deployment tooling, outside the three-layer model.
May import: stdlib, yaml. NEVER world/, sensing/ or dlai/ — this tool
pushes configuration outward; it consumes only deployment artifacts
(data/sensing_layer2.json, data/channel_plan.json, config/rf_profiles.yaml,
an optional serials mapping).

Dry-run is the STRICT default. `--dry-run` builds the full ordered
request plan ({method, path, body} per Meraki Dashboard API v1 call),
prints it as JSON and writes it to data/meraki_request_plan.json with a
provenance block — with NO network I/O whatsoever. `--apply` replays
the SAME plan (single source of truth: `build_plan`) over urllib.

Teammate inputs may not exist yet; both are read when present and fall
back to documented defaults when absent (never fail):
  - config/rf_profiles.yaml  -> per-zone RF profiles
       fallback: one documented default profile (`DEFAULT_RF_PROFILE`)
  - data/channel_plan.json   -> per-sensor 2.4 GHz channel
       fallback: SKIP entry with a warning; channels left to RRM/auto

Secrets: the API key comes ONLY from env MERAKI_API_KEY, never argv or
files. The scanning shared secret is never stored in the plan — the
plan carries the sentinel "ENV:MERAKI_SCANNING_SECRET", resolved from
the environment only at --apply time.

Serial mapping: a real deploy maps sensor_id -> AP serial (--serials
serials.json, shape {"S000": "Q2XX-...", ...}). Without it, per-AP
entries are keyed by sensor_id with "serial": "UNMAPPED" and a
"{{serial:Sxxx}}" placeholder in the path; --apply refuses such a plan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

RF_PROFILES_YAML = Path("config") / "rf_profiles.yaml"
CHANNEL_PLAN_JSON = Path("data") / "channel_plan.json"
SENSORS_JSON = Path("data") / "sensing_layer2.json"
PLAN_OUT_JSON = Path("data") / "meraki_request_plan.json"

SECRET_SENTINEL = "ENV:MERAKI_SCANNING_SECRET"
UNMAPPED = "UNMAPPED"

# Fallback RF profile, used until the RF team lands config/rf_profiles.yaml.
# provenance: assumed — engineering values, superseded by rf_profiles.yaml.
#   - 2.4 GHz is the positioning band (CLAUDE.md §2: 95.5% >=3-AP vs 66/64.7);
#     validAutoChannels 1/6/11 keeps the non-overlapping plan until
#     data/channel_plan.json pins per-AP channels.
#   - minPower 8 / maxPower 17 dBm brackets the ~16-17 dBm cart-panel EIRP:
#     the uplink governs the budget, so the downlink must not be allowed to
#     paint coverage the uplink cannot reciprocate (CLAUDE.md §2).
#   - minBitrate 11 at 2.4 sheds sticky far clients without killing the
#     battery panels' low-MCS frames.
DEFAULT_RF_PROFILE: dict[str, Any] = {
    "bandSelectionType": "ap",
    "apBandSettings": {"bandOperationMode": "dual", "bandSteeringEnabled": False},
    "twoFourGhzSettings": {
        "minPower": 8, "maxPower": 17, "minBitrate": 11,
        "axEnabled": True, "validAutoChannels": [1, 6, 11],
    },
    "fiveGhzSettings": {
        "minPower": 8, "maxPower": 17, "minBitrate": 12,
        "channelWidth": "20", "validAutoChannels": [36, 40, 44, 48],
    },
}
DEFAULT_PROFILE_ZONE = "default"


def _profile_name(zone: str) -> str:
    return f"centro-{zone.replace('_', '-')}"


def load_sensors(repo_root: Path | None = None) -> list[dict]:
    """The 150 logical positions from the locked deployment artifact."""
    root = repo_root or REPO_ROOT
    with open(root / SENSORS_JSON, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return sorted(doc["sensors"], key=lambda s: s["sensor_id"])


def load_rf_profiles(repo_root: Path | None = None) -> tuple[dict[str, dict], str]:
    """Per-zone RF profiles from config/rf_profiles.yaml, when present.

    Accepted shapes: {"profiles": {zone: {...}}} or a top-level mapping
    {zone: {...}}. Returns ({zone: profile_body}, source_note).
    Fallback (file absent/empty): one documented default profile bound
    to every zone — the tool must not fail while the RF team iterates.
    """
    root = repo_root or REPO_ROOT
    path = root / RF_PROFILES_YAML
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        profiles = doc.get("profiles", doc)
        if profiles:
            return ({z: dict(p) for z, p in profiles.items()},
                    str(RF_PROFILES_YAML))
    return ({DEFAULT_PROFILE_ZONE: dict(DEFAULT_RF_PROFILE)},
            f"fallback:DEFAULT_RF_PROFILE ({RF_PROFILES_YAML} absent)")


def load_channel_plan(repo_root: Path | None = None) -> tuple[dict[str, int] | None, str]:
    """Per-sensor 2.4 GHz channels from data/channel_plan.json, when present.

    Accepted shapes:
      - {"assignments": [{"sensor_id": "S000", "channel": 1}, ...]}
        (the channel planner's actual output)
      - {"channels": {sensor_id: ch}} or a flat mapping {sensor_id: ch}
    Returns (mapping | None, source_note); None means the plan gets a
    single SKIP/warning entry instead of per-AP channels.
    """
    root = repo_root or REPO_ROOT
    path = root / CHANNEL_PLAN_JSON
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("assignments"), list):
            channels = {str(a["sensor_id"]): int(a["channel"])
                        for a in doc["assignments"]}
        else:
            raw = doc.get("channels", doc) if isinstance(doc, dict) else {}
            channels = {str(k): int(v) for k, v in raw.items()
                        if isinstance(v, (int, float, str))
                        and str(v).isdigit()}
        if channels:
            return channels, str(CHANNEL_PLAN_JSON)
    return None, f"absent:{CHANNEL_PLAN_JSON}"


def load_serials(path: str | Path | None) -> tuple[dict[str, str], str]:
    """Optional sensor_id -> AP serial mapping ({"S000": "Q2AB-..."})."""
    if path is None:
        return {}, "absent (per-AP entries marked serial: UNMAPPED)"
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return {str(k): str(v) for k, v in doc.items()}, str(path)


def build_plan(*, org_id: str = "{{orgId}}", network_id: str = "{{networkId}}",
               scanning_url: str = "https://twin.example.invalid/scanning/v3",
               validator: str = "centro-validator",
               syslog_host: str = "127.0.0.1", syslog_port: int = 6514,
               serials: dict[str, str] | None = None,
               repo_root: Path | None = None) -> dict:
    """Build the full ordered request plan. Pure and deterministic.

    Single source of truth: both --dry-run (print/write) and --apply
    (urllib) consume exactly this output. No network I/O here, no
    timestamps — same inputs, byte-identical plan (repo style §10).
    """
    root = repo_root or REPO_ROOT
    serials = serials or {}
    sensors = load_sensors(root)
    profiles, profiles_src = load_rf_profiles(root)
    channels, channels_src = load_channel_plan(root)
    fallbacks: list[str] = []

    requests: list[dict] = []

    # 1) RF profiles — created first so per-AP bindings can reference them.
    for zone in sorted(profiles):
        body = dict(profiles[zone])
        body["name"] = _profile_name(zone)
        requests.append({
            "method": "POST",
            "path": f"/networks/{network_id}/wireless/rfProfiles",
            "body": body,
            "note": f"RF profile for zone '{zone}' (source: {profiles_src})",
        })
    if profiles_src.startswith("fallback:"):
        fallbacks.append(f"rf_profiles: {profiles_src}")

    # 2) Per-AP radio settings: profile binding by zone (+ 2.4 GHz channel
    #    when the channel plan exists). "{{rfProfileId:...}}" is resolved
    #    at --apply time from the profile-creation responses.
    if channels is None:
        fallbacks.append(f"channel_plan: {channels_src}")
        requests.append({
            "method": "SKIP", "path": None, "body": None,
            "note": ("WARNING: data/channel_plan.json absent — per-AP "
                     "2.4 GHz channels left to RRM/auto (Tx power moves "
                     "2-26 dBm every 30 min, CLAUDE.md §5); re-run when "
                     "the channel planner lands"),
        })
    zones_seen = {s["zone"] for s in sensors}
    missing_zones = sorted(zones_seen - set(profiles))
    if missing_zones:
        # A zone the RF team has not covered binds to the default profile;
        # create it too if it was not already in the plan.
        if DEFAULT_PROFILE_ZONE not in profiles:
            fallbacks.append(
                f"zones without a profile bind to default: {missing_zones}")
            requests.insert(len(profiles), {
                "method": "POST",
                "path": f"/networks/{network_id}/wireless/rfProfiles",
                "body": {**DEFAULT_RF_PROFILE,
                         "name": _profile_name(DEFAULT_PROFILE_ZONE)},
                "note": f"fallback profile for uncovered zones {missing_zones}",
            })

    for s in sensors:
        sid = s["sensor_id"]
        zone = s["zone"] if s["zone"] in profiles else DEFAULT_PROFILE_ZONE
        serial = serials.get(sid, UNMAPPED)
        path_serial = serial if serial != UNMAPPED else f"{{{{serial:{sid}}}}}"
        body: dict[str, Any] = {
            "rfProfileId": f"{{{{rfProfileId:{_profile_name(zone)}}}}}",
        }
        note = f"bind {sid} (zone {s['zone']}) to {_profile_name(zone)}"
        if channels is not None:
            if sid in channels:
                body["twoFourGhzSettings"] = {"channel": channels[sid]}
                note += f"; 2.4 GHz channel {channels[sid]} ({channels_src})"
            else:
                note += "; WARNING: no channel in channel_plan.json"
        requests.append({
            "method": "PUT",
            "path": f"/devices/{path_serial}/wireless/radio/settings",
            "body": body,
            "sensor_id": sid,
            "serial": serial,
            "note": note,
        })

    # 3) Air Marshal alerting + syslog export.
    requests.append({
        "method": "PUT",
        "path": f"/networks/{network_id}/alerts/settings",
        "body": {"alerts": [{"type": "rogueAp", "enabled": True,
                             "alertDestinations": {"allAdmins": True}}]},
        "note": "Air Marshal rogue-AP alerting (wiredMacs separates rogue "
                "from the five neighbour networks, CLAUDE.md §5)",
    })
    requests.append({
        "method": "PUT",
        "path": f"/networks/{network_id}/syslogServers",
        "body": {"servers": [{"host": syslog_host, "port": syslog_port,
                              "roles": ["Air Marshal events",
                                        "Security events"]}]},
        "note": "syslog export; aWIPS syslog carries NO client identity and "
                "is throttled per signature/AP/interval — modelled as gaps, "
                "not bugs (CLAUDE.md §5)",
    })

    # 4) Scanning API v3: enable, then register the receiver. The shared
    #    secret is NEVER stored in the plan — sentinel resolved at apply.
    requests.append({
        "method": "PUT",
        "path": f"/networks/{network_id}/wireless/location/scanning",
        "body": {"analyticsEnabled": True, "scanningApiEnabled": True},
        "note": "enable location analytics + Scanning API",
    })
    requests.append({
        "method": "POST",
        "path": f"/networks/{network_id}/wireless/location/scanning/receivers",
        "body": {"url": scanning_url, "sharedSecret": SECRET_SENTINEL,
                 "validator": validator, "apiVersion": "3",
                 "radioType": "WiFi"},
        "note": "Scanning API v3 receiver (cisco/scanning_receiver.py); "
                "GET on the url must echo the validator (Meraki handshake)",
    })

    unmapped = sum(1 for r in requests if r.get("serial") == UNMAPPED)
    provenance = {
        "tool": "cisco/meraki_provision.py",
        "plan_version": 1,
        "org_id": org_id,
        "network_id": network_id,
        "inputs": {
            "sensors": f"{SENSORS_JSON} ({len(sensors)} logical positions)",
            "rf_profiles": profiles_src,
            "channel_plan": channels_src,
            "serials": ("provided" if serials
                        else "absent (per-AP entries marked serial: UNMAPPED)"),
        },
        "fallbacks": fallbacks,
        "unmapped_serials": unmapped,
        "request_count": len(requests),
        "secrets": "api key: env MERAKI_API_KEY only; scanning secret: "
                   f"sentinel {SECRET_SENTINEL}, resolved at --apply, "
                   "never written to this file",
    }
    return {"provenance": provenance, "requests": requests}


# ---------------------------------------------------------------- apply ----

def _resolve_refs(node: Any, profile_ids: dict[str, str]) -> Any:
    """Resolve {{rfProfileId:...}} and the secret sentinel at apply time."""
    if isinstance(node, dict):
        return {k: _resolve_refs(v, profile_ids) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, profile_ids) for v in node]
    if isinstance(node, str):
        if node == SECRET_SENTINEL:
            secret = os.environ.get("MERAKI_SCANNING_SECRET")
            if not secret:
                raise RuntimeError("MERAKI_SCANNING_SECRET not set in env")
            return secret
        if node.startswith("{{rfProfileId:") and node.endswith("}}"):
            name = node[len("{{rfProfileId:"):-2]
            if name not in profile_ids:
                raise RuntimeError(f"rfProfileId for {name!r} not yet created")
            return profile_ids[name]
    return node


def apply_plan(plan: dict, api_key: str,
               base_url: str = "https://api.meraki.com/api/v1",
               opener: Any | None = None) -> list[dict]:
    """Replay the plan over HTTPS. Thin by design — untestable live here.

    `opener` (urlopen-compatible) is injectable so tests never open a
    socket. Refuses plans with UNMAPPED serials: a placeholder path must
    never reach the wire.
    """
    import urllib.request
    open_ = opener or urllib.request.urlopen
    unmapped = [r["sensor_id"] for r in plan["requests"]
                if r.get("serial") == UNMAPPED]
    if unmapped:
        raise RuntimeError(
            f"{len(unmapped)} per-AP entries have serial: UNMAPPED "
            f"(e.g. {unmapped[:3]}) — provide --serials serials.json")
    profile_ids: dict[str, str] = {}
    results: list[dict] = []
    for entry in plan["requests"]:
        if entry["method"] == "SKIP":
            results.append({"skipped": entry.get("note")})
            continue
        body = _resolve_refs(entry.get("body"), profile_ids)
        req = urllib.request.Request(
            base_url + entry["path"],
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=entry["method"],
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        with open_(req, timeout=30) as resp:
            raw = resp.read()
        payload = json.loads(raw) if raw else {}
        if (entry["method"] == "POST"
                and entry["path"].endswith("/wireless/rfProfiles")):
            profile_ids[entry["body"]["name"]] = str(payload.get("id"))
        results.append({"method": entry["method"], "path": entry["path"],
                        "status": getattr(resp, "status", None)})
    return results


# ----------------------------------------------------------------- CLI ----

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision the Centro CW9172I deployment via the Meraki "
                    "Dashboard API v1. Dry-run (no network I/O) is the "
                    "strict default.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="build/print/write the plan; NO network I/O "
                           "(default)")
    mode.add_argument("--apply", action="store_true",
                      help="execute the plan; requires env MERAKI_API_KEY "
                           "and real --org-id/--network-id")
    ap.add_argument("--org-id", default="{{orgId}}")
    ap.add_argument("--network-id", default="{{networkId}}")
    ap.add_argument("--scanning-url",
                    default="https://twin.example.invalid/scanning/v3",
                    help="POST URL of cisco/scanning_receiver.py")
    ap.add_argument("--validator", default="centro-validator",
                    help="handshake string the receiver echoes on GET "
                         "(public, argv-safe; the secret is env-only)")
    ap.add_argument("--syslog-host", default="127.0.0.1")
    ap.add_argument("--syslog-port", type=int, default=6514)
    ap.add_argument("--serials", type=Path, default=None,
                    help="JSON mapping sensor_id -> AP serial; absent => "
                         "plan entries marked serial: UNMAPPED")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"plan output path (default {PLAN_OUT_JSON})")
    args = ap.parse_args(argv)

    serials, _serials_src = load_serials(args.serials)
    plan = build_plan(org_id=args.org_id, network_id=args.network_id,
                      scanning_url=args.scanning_url, validator=args.validator,
                      syslog_host=args.syslog_host, syslog_port=args.syslog_port,
                      serials=serials)

    if not args.apply:                       # dry-run: the strict default
        out = args.out or (REPO_ROOT / PLAN_OUT_JSON)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=1)
            fh.write("\n")
        print(json.dumps(plan, indent=1))
        print(f"\n# dry-run: {plan['provenance']['request_count']} requests "
              f"written to {out} — no network I/O performed", file=sys.stderr)
        return 0

    api_key = os.environ.get("MERAKI_API_KEY")
    if not api_key:
        ap.error("--apply requires env MERAKI_API_KEY (never argv/files)")
    if args.network_id.startswith("{{") or args.org_id.startswith("{{"):
        ap.error("--apply requires real --org-id and --network-id")
    results = apply_plan(plan, api_key)
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
