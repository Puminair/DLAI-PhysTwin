"""Cisco tooling: dry-run plan builder + Scanning API v3 receiver.

No sockets are opened anywhere in this file — the receiver is tested
through its pure functions, and apply_plan is never driven live.
Fallback behaviour is exercised against a synthetic tmp_path repo root
(monkeypatching cisco.meraki_provision.REPO_ROOT); the real data/ and
config/ files are never touched or deleted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cisco import meraki_provision as mp
from cisco import scanning_receiver as sr

REPO = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------- fixtures ----

@pytest.fixture()
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal synthetic repo root: sensors only, no teammate files."""
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    sensors = {"provenance": {"note": "synthetic test fixture"},
               "sensors": [
                   {"sensor_id": "S000", "x": 1.0, "y": 1.0, "z": 3.0,
                    "zone": "perimeter", "model": "CW9172I", "units": 2},
                   {"sensor_id": "S001", "x": 2.0, "y": 2.0, "z": 3.0,
                    "zone": "sales_floor", "model": "CW9172I", "units": 2},
                   {"sensor_id": "S002", "x": 3.0, "y": 3.0, "z": 3.0,
                    "zone": "back_of_house", "model": "CW9172I", "units": 2},
               ]}
    (tmp_path / "data" / "sensing_layer2.json").write_text(
        json.dumps(sensors), encoding="utf-8")
    monkeypatch.setattr(mp, "REPO_ROOT", tmp_path)
    return tmp_path


# ------------------------------------------------------- plan builder ----

def test_dry_run_plan_is_deterministic_against_real_repo():
    kwargs = dict(network_id="N_123", org_id="O_9")
    a = mp.build_plan(**kwargs)
    b = mp.build_plan(**kwargs)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), (
        "same inputs must yield a byte-identical plan")
    # every deployed logical position gets a per-AP radio-settings entry —
    # count is read from the live layout, not hardcoded (it grows with
    # reallocation and operator-added Meraki units)
    import json as _json
    n_sensors = len(_json.loads(
        (mp.REPO_ROOT / "data" / "sensing_layer2.json").read_text())["sensors"])
    per_ap = [r for r in a["requests"]
              if r["method"] == "PUT" and "radio/settings" in (r["path"] or "")]
    assert len(per_ap) == n_sensors
    assert a["provenance"]["request_count"] == len(a["requests"])


def test_plan_contains_rf_profile_creation_and_scanning_config():
    plan = mp.build_plan(network_id="N_123")
    profile_posts = [r for r in plan["requests"]
                     if r["method"] == "POST"
                     and r["path"] == "/networks/N_123/wireless/rfProfiles"]
    assert profile_posts, "RF profile creation must be in the plan"
    assert all("name" in r["body"] for r in profile_posts)

    scan = [r for r in plan["requests"]
            if r["path"] == "/networks/N_123/wireless/location/scanning/receivers"]
    assert len(scan) == 1
    assert scan[0]["body"]["apiVersion"] == "3"
    assert scan[0]["body"]["url"].startswith("https://")
    # the shared secret must never appear in the plan — sentinel only
    assert scan[0]["body"]["sharedSecret"] == mp.SECRET_SENTINEL
    enable = [r for r in plan["requests"]
              if r["path"] == "/networks/N_123/wireless/location/scanning"]
    assert enable and enable[0]["body"]["scanningApiEnabled"] is True

    assert any((r["path"] or "").endswith("/syslogServers")
               for r in plan["requests"]), "Air Marshal syslog export"
    assert any((r["path"] or "").endswith("/alerts/settings")
               for r in plan["requests"]), "Air Marshal alerting"


def test_fallbacks_when_teammate_files_absent(tmp_repo: Path):
    plan = mp.build_plan(network_id="N_1")   # REPO_ROOT monkeypatched
    prov = plan["provenance"]
    assert prov["inputs"]["rf_profiles"].startswith("fallback:")
    assert prov["inputs"]["channel_plan"].startswith("absent:")
    assert any("rf_profiles" in f for f in prov["fallbacks"])
    assert any("channel_plan" in f for f in prov["fallbacks"])
    # exactly one default profile, and a SKIP warning entry for channels
    posts = [r for r in plan["requests"] if r["method"] == "POST"
             and (r["path"] or "").endswith("/wireless/rfProfiles")]
    assert len(posts) == 1 and posts[0]["body"]["name"] == "centro-default"
    skips = [r for r in plan["requests"] if r["method"] == "SKIP"]
    assert len(skips) == 1 and "channel_plan.json" in skips[0]["note"]
    # every zone binds to the default profile
    binds = [r for r in plan["requests"] if "radio/settings" in (r["path"] or "")]
    assert len(binds) == 3
    assert all(r["body"]["rfProfileId"] == "{{rfProfileId:centro-default}}"
               for r in binds)
    assert all("twoFourGhzSettings" not in r["body"] for r in binds), (
        "no channel plan -> no channel assignments, only the warning")


def test_teammate_files_used_when_present(tmp_repo: Path):
    (tmp_repo / "config" / "rf_profiles.yaml").write_text(
        "profiles:\n"
        "  sales_floor: {bandSelectionType: ap}\n"
        "  back_of_house: {bandSelectionType: ap}\n"
        "  perimeter: {bandSelectionType: ap}\n", encoding="utf-8")
    # the channel planner's real output shape: an assignments list
    (tmp_repo / "data" / "channel_plan.json").write_text(
        json.dumps({"band_ghz": 2.4, "assignments": [
            {"sensor_id": "S000", "channel": 1},
            {"sensor_id": "S001", "channel": 6},
            {"sensor_id": "S002", "channel": 11}]}),
        encoding="utf-8")
    plan = mp.build_plan(network_id="N_1")
    names = {r["body"]["name"] for r in plan["requests"]
             if r["method"] == "POST"
             and (r["path"] or "").endswith("/wireless/rfProfiles")}
    assert names == {"centro-sales-floor", "centro-back-of-house",
                     "centro-perimeter"}
    assert not [r for r in plan["requests"] if r["method"] == "SKIP"]
    ch = {r["sensor_id"]: r["body"]["twoFourGhzSettings"]["channel"]
          for r in plan["requests"] if "radio/settings" in (r["path"] or "")}
    assert ch == {"S000": 1, "S001": 6, "S002": 11}
    assert plan["provenance"]["fallbacks"] == []

    # the simple {"channels": {...}} shape is accepted too
    (tmp_repo / "data" / "channel_plan.json").write_text(
        json.dumps({"channels": {"S000": 11}}), encoding="utf-8")
    channels, src = mp.load_channel_plan(tmp_repo)
    assert channels == {"S000": 11} and src.endswith("channel_plan.json")


def test_serials_unmapped_marker_and_mapping(tmp_repo: Path):
    plan = mp.build_plan(network_id="N_1")
    per_ap = [r for r in plan["requests"] if "radio/settings" in (r["path"] or "")]
    assert all(r["serial"] == "UNMAPPED" for r in per_ap)
    assert all("{{serial:" in r["path"] for r in per_ap), (
        "unmapped serials must stay visible placeholders, never fake serials")

    plan2 = mp.build_plan(network_id="N_1",
                          serials={"S000": "Q2AB-0000-0001"})
    by_id = {r["sensor_id"]: r for r in plan2["requests"]
             if "radio/settings" in (r["path"] or "")}
    assert by_id["S000"]["serial"] == "Q2AB-0000-0001"
    assert by_id["S000"]["path"] == "/devices/Q2AB-0000-0001/wireless/radio/settings"
    assert by_id["S001"]["serial"] == "UNMAPPED"

    # --apply must refuse a plan with unmapped serials, before any I/O
    with pytest.raises(RuntimeError, match="UNMAPPED"):
        mp.apply_plan(plan, api_key="k",
                      opener=lambda *a, **k: pytest.fail("network I/O!"))


def test_dry_run_cli_writes_plan_with_provenance_no_network(
        tmp_repo: Path, capsys: pytest.CaptureFixture):
    rc = mp.main(["--dry-run", "--network-id", "N_1"])
    assert rc == 0
    out_path = tmp_repo / "data" / "meraki_request_plan.json"
    assert out_path.exists()
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["provenance"]["tool"] == "cisco/meraki_provision.py"
    assert doc["provenance"]["request_count"] == len(doc["requests"])
    assert "MERAKI_SCANNING_SECRET" in doc["provenance"]["secrets"]
    printed = capsys.readouterr().out
    assert json.loads(printed) == doc, "printed plan == written plan"


def test_apply_requires_env_api_key(tmp_repo: Path,
                                    monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MERAKI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        mp.main(["--apply", "--org-id", "O_1", "--network-id", "N_1"])


# ------------------------------------------------- scanning receiver ----

SECRET = "s3cr3t-for-tests"


def _post_body(records: list[dict], delivered: int = 2000,
               secret: str = SECRET) -> bytes:
    return json.dumps({"secret": secret, "version": "3.0",
                       "type": "DevicesSeen",
                       "data": {"deliveredAt": delivered,
                                "records": records}}).encode("utf-8")


def test_handshake_echoes_validator():
    assert sr.handshake_body("centro-validator") == b"centro-validator"


def test_wrong_secret_rejected_403():
    status, payload = sr.handle_post(_post_body([], secret="wrong"), SECRET)
    assert status == 403
    assert "error" in payload
    status, _ = sr.handle_post(json.dumps({"data": {}}).encode(), SECRET)
    assert status == 403, "missing secret is a mismatch, not a crash"


def test_invalid_json_rejected_400():
    status, payload = sr.handle_post(b"{not json", SECRET)
    assert status == 400 and "error" in payload


def test_valid_post_normalises_sign_flip_via_dlai_ingest():
    rec = {"stream": "scanning_api_v3/DevicesSeen",
           "clientMac": "0c:8b:7d:00:00:01", "seenTime": 1000,
           "locations": [], "manufacturer": "RetailPanel Ltd",
           "rssiRecords": [{"apMac": "00:2a:10:00:2a:00", "rssi": 58.0}],
           "gap": ["no_location_lt_min_aps"]}
    status, payload = sr.handle_post(_post_body([rec]), SECRET)
    assert status == 200 and len(payload) == 1
    out = payload[0]
    # DevicesSeen positive convention -> dBm negative, and it is RECORDED
    assert out["rssi_dbm"] == [("00:2a:10:00:2a:00", -58.0)]
    assert any("rssi_sign_flipped" in n for n in out["normalisation"]), (
        "the sign-flip must be recorded, not silent (provenance rule)")
    assert out["mac"] == "0c:8b:7d:00:00:01"
    assert out["delivered_at_ms"] == 2000
    assert "no_location_lt_min_aps" in out["gaps"]


def test_meraki_observations_shape_is_adapted():
    body = {"secret": SECRET, "sentAt": 3333,
            "data": {"observations": [
                {"clientMac": "aa:bb:cc:00:00:01", "seenTime": 3000,
                 "locations": [],
                 "rssiRecords": [{"apMac": "m1", "rssi": 61.0}],
                 "gap": []}]}}
    status, payload = sr.handle_post(json.dumps(body).encode(), SECRET)
    assert status == 200
    assert payload[0]["rssi_dbm"] == [("m1", -61.0)]
    assert payload[0]["delivered_at_ms"] == 3333, "sentAt is the fallback"


def test_jsonl_sink_appends(tmp_path: Path):
    sink = tmp_path / "out" / "scanning_ingest.jsonl"
    status, payload = sr.handle_post(_post_body([
        {"stream": "scanning_api_v3/DevicesSeen", "clientMac": "m",
         "seenTime": 1, "locations": [],
         "rssiRecords": [{"apMac": "a", "rssi": 50}], "gap": []}]), SECRET)
    assert status == 200
    sr.append_jsonl(payload, sink)
    sr.append_jsonl(payload, sink)
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["rssi_dbm"] == [["a", -50.0]]
