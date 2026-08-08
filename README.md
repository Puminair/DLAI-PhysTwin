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
python -m pytest tests/ -q             # 37 tests incl. layer-separation enforcement
python -m eval.blind_test --hours 0.25 # the point of all of it
python scripts/build_coverage.py       # coverage report, all bands (~30 s)
python -m viz.server                   # live twin at http://localhost:8787/
```

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

| band | sales ≥3 AP | sales blind | BOH ≥3 AP | BOH blind |
|---|---|---|---|---|
| 2.4 | 99.5 % | 0.5 % | 95.4 % | 3.7 % |
| 5 | 99.5 % | 0.5 % | 66.9 % | 17.2 % |
| 6 | 99.3 % | 0.5 % | 66.0 % | 17.8 % |

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
- **Gaps are the system.** Unassociated randomised MACs never reach the API;
  POSTs are jittered 60-180 s; aWIPS carries no client identity and throttling
  destroys intensity; every record says what was withheld (`gap`) and whether
  it is `observed` or `inferred`.
- **Cold rooms are opaque** (`null`, not a big number): metal is a conductor.
- Inside/outside discrimination is **not built** — a modem 8 m outside is heard
  by dozens of sensors; the perimeter units cannot separate it (§9). No feature
  pretends otherwise.
