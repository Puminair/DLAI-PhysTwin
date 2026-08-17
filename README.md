# Yochananof Centro Rehovot — Live 3D Digital Twin

A physics-grounded digital twin of a 5,000 m² supermarket, built to answer one
question honestly: **can 150 ceiling sensors position battery-powered cart
panels at 1.00 m height, and where can they not?**

## The one rule that governs everything

Three layers. Each consumes only the layer below it. Never skip a layer.

```
Layer 1  PHYSICAL WORLD   world/     geometry, entities, motion, time    ← ground truth
Layer 2  SENSING          sensing/   what can be observed, and how badly ← observations only
Layer 3  DLAI             dlai/      agents, attack path, decision       ← consumes L2 only
```

- Layer 1 is describable without mentioning a single sensor.
- Layer 2 never emits facts — only observations, with noise, latency and gaps.
- Layer 3 **must not import `world/`** — enforced by
  `tests/test_layer_separation.py` at the AST level, including local imports.
- The identity trap: a cart has `cart_id` (a fact, Layer 1); a panel has a MAC
  (an observation, Layer 2). The link lives privately in
  `sensing/pipeline.py` and is exposed by `truth_links()` to `eval/` only, so
  the blind test can score Layer 3's inference without Layer 3 ever seeing it.

## Quick start

```bash
pip install pyyaml numpy pytest        # numpy optional but 100x faster geometry
python scripts/generate_data.py        # regenerate the parametric data/ (deterministic)
python -m pytest tests/ -q             # full suite incl. layer-separation enforcement
python -m eval.blind_test --hours 0.25 # the point of all of it
python scripts/build_coverage.py       # coverage report, all bands (~30 s)
python -m viz.server                   # SIMULATION twin (ground truth) http://localhost:8787/
python scripts/capture_observations.py # capture a Layer-2 stream (or use a real receiver sink)
python -m viz.live_server              # LIVE view (Layer-3 inference only) http://localhost:8788/
python scripts/run_attack.py --list    # attack the twin and watch DLAI react
python scripts/run_attack.py --all     # every catalogue attack, end to end
```

## Attacking the twin — watch DLAI work

`scripts/run_attack.py` closes the loop: pick an attack from
`config/attack_catalog.yaml`, inject it into the twin, and see exactly what
Layer 3 does with it. Injection is faithful to each attack's nature and the
runner prints the pipeline it traverses:

- **`cart_mac_clone` runs the full physical stack** — the real `WorldSim`
  (Layer 1) through the real sensing pipeline (Layer 2) locates an actual cart
  panel, then the runner clones its MAC to a device 45 m away and Layer 3's
  `cart_mac_duplicate` fires (`L1 world → L2 sensing → L3 DLAI`).
- **Network / RF attacks** (rogue on wire, POS-VLAN bridge, evil twin, deauth,
  containment, IP-camera pivot) are emitted as the Layer-2 security records
  sensing produces, routed through the attack analyzer and alert engine.
- **Blind-spot attacks** produce no detection — the runner shows the
  catalogue's reason and the mitigation instead of faking one.

Every emission carries its assessment, attack path, `confidence`
(observed/inferred), listed blind spots, and a RECOMMEND_ONLY action — DLAI
recommends, never enforces. `--all` runs all 23 classes and tallies how many
drove DLAI live vs. how many are structural blind spots shown honestly.

## Two views: the simulation and the live branch

CLAUDE.md distinguishes the simulated world from *the live branch*, and both
are here:

- **`viz/server.py` — the SIMULATION view.** Runs `WorldSim` (Layer 1) and
  shows ground truth: true carts, true shoppers, the coverage cloud, and a
  **live traffic dashboard** — people/carts/staff counts, entry & exit rates
  per minute, checkout throughput, and rolling sparklines of shoppers and
  carts over time (palette validated with the dataviz method). This is what
  you debug the physics against.
- **`viz/live_server.py` — the LIVE view.** Has **no Layer 1 at all** — it
  imports neither `world/` nor `sensing/` (enforced by `tests/test_live.py`).
  It consumes a Layer-2 observation stream (the JSONL sink written by
  `cisco/scanning_receiver.py` from real Meraki Scanning API v3 webhooks, or a
  capture from `scripts/capture_observations.py`), runs it through Layer 3
  (`normalise_batch → EntityResolver → policy/attack → AlertEngine`), and
  renders **only what the engine infers**: positions with their variance ring,
  the sensors hearing each device, and live DLAI recommendations. Every dot is
  labelled *inferred* — never a true location. This is the blind data path
  driving a live picture, which is the entire thesis: what can the store
  actually know from its sensors, with the truth taken away.

## Provenance of `data/`

CLAUDE.md describes `data/` as locked inputs, but this repository began empty —
no data directory existed in any branch or history. `scripts/generate_data.py`
therefore **creates the dataset parametrically** from the locked constants in
`config/defaults.yaml` (fixed seed, byte-reproducible). Every file carries a
provenance block. Interior fit-out is a parametric model, not a survey
(CLAUDE.md §9): 385 entities against the surveyed dataset's 436, with the
verified constants (envelope ring, 2,782 m² sales floor, x-split at 50.58 m,
exactly 77 columns, 150 logical sensors as 18 perimeter + 104 sales + 28 BOH)
honoured exactly. If the surveyed v1.0 dataset ever materialises, drop it into
`data/` and delete the generator's outputs — nothing in the code assumes the
parametric layout.

## What the model shows (this dataset, 2 m grid, cart-panel height 1.00 m)

Current layout (153 logical positions): the 8-unit perimeter→BOH
reallocation is EXECUTED (`scripts/apply_reallocation.py`) and 3
operator-added Meraki-managed units fill the NE warehouse gaps
(`scripts/add_sensors.py`). v0.2 baseline (150, pre-reallocation) in
parentheses:

| band | sales ≥3 AP | sales blind | BOH ≥3 AP | BOH blind |
|---|---|---|---|---|
| 2.4 | 99.5 % | 0.5 % | 96.3 % (95.4) | 3.7 % (3.7) |
| 5 | 99.3 % (99.5) | 0.5 % | 80.7 % (66.9) | 4.6 % (17.2) |
| 6 | 99.1 % (99.3) | 0.5 % | 79.8 % (66.0) | 4.6 % (17.8) |

The BOH story across the two changes: 5 GHz ≥3-AP went 66.9 → 74.8
(reallocation) → 80.7 % (+3 units), blind 17.2 → 6.1 → 4.6 %. What is
left is cold-room shadow — opaque conductors no placement reaches.

Two spec findings reproduce cleanly: **2.4 GHz wins decisively** wherever
gondola crossings dominate, and **back of house collapses at 5/6 GHz** (28
sensors over 2,218 m² — no physics fixes it, only reallocation). One deviates:
the spec's surveyed model reports 95.5/66/64.7 % on the *sales floor*, while
this parametric fit-out over-performs there at 5/6 GHz. The reason is
identifiable: 104 sensors over 2,782 m² is one per 27 m², and the parametric
aisles keep enough intra-aisle line-of-sight segments that three local sensors
almost always survive even 33 dB gondola losses. The surveyed store's fixture
placement evidently breaks aisle LOS more aggressively. This is exactly the
sensitivity §9 warns about — gondola attenuation and fit-out geometry are the
first things to measure, and all of them are runtime-overridable in config to
re-run the question against measured values.

## Layout

```
config/          every `assumed` constant, runtime-overridable — never inline
world/           L1: clock (one, injected), geometry (+numpy fast path),
                 entities, motion, events, sim
sensing/         L2: propagation (uplink budgets), reflections (image method,
                 only where direct fails 3-AP), sensor (CW9172I), observation
                 (Scanning API v3 + Air Marshal shapes, sign conventions),
                 pipeline (jittered POSTs, MAC policy, gaps), aireye (security
                 streams: throttled aWIPS, wiredMacs discrimination)
dlai/            L3: ingest (sign normalisation, recorded), floorplan
                 (independent artifact reader — the import ban's price),
                 entity (position as distribution, resolution by elimination),
                 interaction, policy (RECOMMEND_ONLY), attack (RECOMMEND_ONLY)
viz/             stdlib websocket server + three.js r128 twin; coverage cloud
                 at 1.00 m; carts heard by <3 sensors turn red — the thesis
eval/            blind_test (truth vs inference), metrics, coverage
scripts/         generate_data, build_coverage
tests/           37 tests; layer separation enforced at AST level
```

## Expert-team deliverables

Four specialist work products sit on top of the twin, each grounded in its
physics and each carrying provenance:

- **Network design** (`docs/network_design.md`, `scripts/plan_channels.py` →
  `data/channel_plan.json`): deterministic 1/6/11 channel plan for the 150
  positions (balanced 50/50/50, min co-channel spacing 4.63 m — 8 m is
  provably infeasible for this layout), PoE budget (300 units → 8× 48-port
  UPOE switches with N+1, pair-split so a switch failure never costs a
  logical position), VLAN plan tied to Air Marshal wiredMacs visibility.
- **Contextual alerts** (`config/alerts.yaml`, `dlai/alerts.py`): 11 alerts
  defined by this building's context — and two documented non-alerts (RRM
  downlink swings; any inside/outside claim). RECOMMEND_ONLY, blind spots
  listed per alert, layer ban intact.
- **Attack surface** (`config/attack_catalog.yaml`, floating window in the
  twin): 23 store-specific attack classes — the offensive mirror of the alert
  catalog — each with target, why-this-store, severity, the alert that detects
  it (or an honest **blind spot** with its reason), and a RECOMMEND_ONLY
  mitigation. This includes 8 **camera/video-system** attacks: only
  `ip_camera_as_pivot` is detected (it presents a bridge on the wire, caught by
  `rogue_ap_on_wire`); the other seven are honest blind spots, because this is a
  Wi-Fi/RF twin with no video analytics — RTSP/ONVIF hijack, camera
  blinding/tamper, video loop injection, NVR credential compromise, silent
  camera pivot, video-blindspot exploitation, and `rf_video_desync` (acting in
  a cell that is both RF-blind and video-blind). A test asserts every detection
  references a real alert id, every CLAUDE.md-documented limit is catalogued,
  and every camera blind spot names the video/VMS/NVR side rather than faking an
  RF detection. The twin serves it at `/attacks.json` and renders it in a
  draggable, filterable panel (toggle "attack surface ▸").
- **Sensor tuning** (`docs/sensor_tuning.md`, `config/rf_profiles.yaml`,
  `scripts/propose_reallocation.py` → `data/reallocation_proposal.json`):
  per-zone RF profiles (2.4 GHz as the RTLS band, TPC clamped against RRM,
  containment off during trading) and a proven reallocation of 8 perimeter
  units into BOH: at 5 GHz, ≥3-AP 66.9→74.8 % and blind 17.2→6.1 %, for zero
  hardware spend.
- **Cisco integration** (`cisco/`): Meraki Dashboard API provisioner with a
  strict dry-run (157-entry request plan from the team's artifacts) and a
  Scanning API v3 receiver that normalises webhooks straight into the
  Layer-3 ingest path.

## Cameras: a second sensing modality, and coverage fusion

The twin now models CCTV alongside the RF sensors — the same three-layer
discipline, a second modality:

- **Layer 1** (`world/entities.py::Camera`, `data/cameras.json`): 54 parametric
  cameras (domes on aisle centrelines, aisle/dock bullets, entrance PTZ) as
  physical fixtures — placement and aim are world facts, no stream URL or
  credentials.
- **Layer 2** (`sensing/vision.py`): what each camera can actually *see*.
  Mirrors the RF propagation model, but light is **binary** — it does not
  attenuate through a shelf, so any solid fixture on the 3D line of sight blocks
  the view. A camera at 3.20 m looking down clears a 2.00 m gondola only where
  the depression geometry allows: the same lesson the RF model teaches,
  inverted.
- **Fusion** (`eval/coverage_fusion.py`, `scripts/build_fusion.py` →
  `data/fusion_report.json`): every floor cell crossed on two questions — can RF
  position here? can a camera see here? — into four classes: **both**,
  **rf_only** (locate, can't watch), **video_only** (watch, can't locate), and
  **neither**. The twin renders it under the "RF↔video fusion" toggle.

The honest result: on the sales floor RF is so strong that cameras are largely
**redundant** (aisles are `both`, runways are `rf_only`), and the RF-blind
cells sit behind cold rooms where cameras are **also** blocked — so `video_only`
is near zero and a handful of `neither` cells survive between the cold rooms.
Those `neither` cells are the physical realization of the `rf_video_desync`
attack: a place an adversary is neither located nor seen. The real value of the
second modality here is not coverage backfill but **independent corroboration**
for the attack classes RF cannot see at all (camera tampering, RTSP/ONVIF
hijack, video loop injection) — see the attack surface below.

## Design notes worth remembering

- **The uplink governs.** Every budget starts from the cart panel's ~16-17 dBm
  EIRP, never the sensor's 24 dBm. `test_uplink_asymmetry_governs_the_budget`
  keeps it that way.
- **Position is a distribution.** `dlai.entity` produces candidates + variance;
  resolution is by *elimination* (inside a gondola: impossible; outside the
  envelope: impossible; faster than walking: impossible) and the variance is
  never collapsed.
- **Flicker is the metric that matters.** Bodies are counted per-ray as dynamic
  absorbers; tracks count transitions across the 3-AP rule, and the policy
  engine downgrades trust in flickering tracks instead of acting on zigzags.
- **A full staff shift is on the floor** (`world/staffing.py`, counts in
  config): 12 cashiers holding lanes (unstaffed lanes close), 6 stockers, 4
  warehouse workers on racking/dock loops, 4 prep staff, 2 security, 2
  managers crossing between office and floor through the split-wall doorways.
  Staff are bodies (they absorb RF like shoppers — adding the shift measurably
  raises position error and wakes flicker) and their associated phones are
  observed with stable MACs, sampled round-robin rather than per-tick.
- **Gaps are the system.** Unassociated randomised MACs never reach the API;
  POSTs are jittered 60-180 s; aWIPS carries no client identity and throttling
  destroys intensity; every record says what was withheld (`gap`) and whether
  it is `observed` or `inferred`.
- **Cold rooms are opaque** (`null`, not a big number): metal is a conductor.
- Inside/outside discrimination is **not built** — a modem 8 m outside is heard
  by dozens of sensors; the perimeter units cannot separate it (§9). No feature
  pretends otherwise.
