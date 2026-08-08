# cisco/ — Meraki provisioning + Scanning API v3 receiver

Deployment tooling, outside the three-layer model. Nothing here imports
`world/` or `sensing/`; the receiver imports `dlai.ingest` only.

## `meraki_provision.py` — push the plan to a Meraki org

Builds the full ordered request plan (`{method, path, body}` per Meraki
Dashboard API v1 call) for the 150 CW9172I logical positions:

1. create the per-zone RF profiles (`config/rf_profiles.yaml`;
   fallback: one documented default profile, `DEFAULT_RF_PROFILE`),
2. per-AP radio settings — bind each AP to its zone's profile
   (`data/sensing_layer2.json`) and set its 2.4 GHz channel
   (`data/channel_plan.json`; fallback: a single `SKIP` warning entry,
   channels left to RRM/auto),
3. enable Air Marshal rogue-AP alerting + syslog export,
4. enable the Scanning API and register the v3 receiver (URL +
   validator from argparse; secret from env).

`build_plan()` is the **single source of truth**: both `--dry-run` and
`--apply` consume exactly its output, and it is deterministic (no
timestamps, sorted iteration).

### Dry run (the strict default — NO network I/O)

```console
$ python3 cisco/meraki_provision.py --dry-run
{
 "provenance": {
  "tool": "cisco/meraki_provision.py",
  "plan_version": 1,
  "inputs": {
   "sensors": "data/sensing_layer2.json (150 logical positions)",
   "rf_profiles": "config/rf_profiles.yaml",
   "channel_plan": "data/channel_plan.json",
   "serials": "absent (per-AP entries marked serial: UNMAPPED)"
  },
  "fallbacks": [],
  "unmapped_serials": 150,
  "request_count": 157,
  ...
 },
 "requests": [ ... ]
}
# dry-run: 157 requests written to data/meraki_request_plan.json — no network I/O performed
```

A per-AP entry (serials file absent, so `serial: UNMAPPED` and a
visible placeholder in the path — never a fake serial):

```json
{
 "method": "PUT",
 "path": "/devices/{{serial:S000}}/wireless/radio/settings",
 "body": {"rfProfileId": "{{rfProfileId:centro-perimeter}}",
          "twoFourGhzSettings": {"channel": 1}},
 "sensor_id": "S000",
 "serial": "UNMAPPED",
 "note": "bind S000 (zone perimeter) to centro-perimeter; 2.4 GHz channel 1 (data/channel_plan.json)"
}
```

When the teammate files are **absent** the tool does not fail — it
documents the fallback in provenance and the plan itself
(156 requests: one default profile instead of three, plus one warning):

```json
"inputs": {
 "rf_profiles": "fallback:DEFAULT_RF_PROFILE (config/rf_profiles.yaml absent)",
 "channel_plan": "absent:data/channel_plan.json", ...
},
"fallbacks": [
 "rf_profiles: fallback:DEFAULT_RF_PROFILE (config/rf_profiles.yaml absent)",
 "channel_plan: absent:data/channel_plan.json"
]
```
```json
{
 "method": "SKIP", "path": null, "body": null,
 "note": "WARNING: data/channel_plan.json absent — per-AP 2.4 GHz channels left to RRM/auto (Tx power moves 2-26 dBm every 30 min, CLAUDE.md §5); re-run when the channel planner lands"
}
```

### Apply (real org — untested here by design)

```console
$ export MERAKI_API_KEY=...           # env ONLY — never argv, never a file
$ export MERAKI_SCANNING_SECRET=...   # resolved at apply; never stored in the plan
$ python3 cisco/meraki_provision.py --apply \
    --org-id O_xxx --network-id N_xxx \
    --scanning-url https://twin.example.com/scanning/v3 \
    --validator <string-from-dashboard> \
    --serials serials.json            # {"S000": "Q2AB-...", ...}
```

`--apply` refuses a plan containing `serial: UNMAPPED` before any I/O.
`{{rfProfileId:centro-<zone>}}` references are resolved from the
profile-creation responses at apply time; `ENV:MERAKI_SCANNING_SECRET`
is resolved from the environment.

## `scanning_receiver.py` — Scanning API v3 webhook endpoint

The Meraki handshake contract:

- `GET  <endpoint>` → `200 text/plain`, body = the configured
  **validator** string (Meraki verifies URL ownership this way).
- `POST <endpoint>` → JSON `{"secret": ..., "data": {...}}`.
  Wrong/missing secret → **403** (constant-time compare). Invalid JSON
  → **400**. Accepted → records normalised and appended to the JSONL
  sink, response `{"accepted": N, "sink": ...}`.

```console
$ export MERAKI_SCANNING_SECRET=...   # or the tool prompts; never argv
$ python3 -m cisco.scanning_receiver --port 8123 \
    --validator centro-validator --sink data/scanning_ingest.jsonl
```

### How the data flows into dlai

```
Meraki POST {"secret", "data"}                      (cisco/scanning_receiver.py)
  └─ adapt_meraki_body()  → {"deliveredAt": ms, "records": [...]}
       └─ dlai.ingest.normalise_batch()  → NormalisedObservation
            • DevicesSeen POSITIVE RSSI → dBm (negative), and the flip is
              RECORDED: normalisation = ["rssi_sign_flipped:DevicesSeen_positive_to_dbm"]
            • gaps / manufacturer / cloud_location carried through
       └─ appended to the JSONL sink, one record per line
            └─ dlai.entity.EntityResolver.consume(obs)  → position as a
               distribution (candidates + variance, resolved by elimination)
```

Request handling is factored into pure functions (`handshake_body`,
`secret_ok`, `adapt_meraki_body`, `handle_post`, `append_jsonl`) so
`tests/test_cisco.py` exercises the full contract without ever opening
a socket; only `__main__` wires `http.server`.

## Security notes

- The **Meraki API key** comes ONLY from env `MERAKI_API_KEY` — never
  argv (visible in `ps`/shell history), never a file in the repo.
- The **scanning shared secret** likewise: env `MERAKI_SCANNING_SECRET`
  or an interactive prompt. It is never written into
  `data/meraki_request_plan.json` — the plan carries the sentinel
  `ENV:MERAKI_SCANNING_SECRET`, resolved only at `--apply` time.
- The **validator** is public by contract (echoed to any GET), so it is
  argv-safe.

## Tests

```console
$ python3 -m pytest tests/test_cisco.py -q
13 passed
```
