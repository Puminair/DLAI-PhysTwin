# CLAUDE.md — Yochananof Centro Rehovot · Live 3D Digital Twin
Build spec for Claude Code. Read this file completely before writing any code.
---
## 0. The one rule that governs everything
Three layers. Each consumes only the layer below it. **Never skip a layer.**
```
Layer 1  PHYSICAL WORLD   geometry, entities, motion, time    ← ground truth
Layer 2  SENSING          what can be observed, and how badly ← observations only
Layer 3  DLAI             agents, attack path, decision       ← consumes L2 only
```
Concretely:
- Layer 1 must be describable **without mentioning a single sensor**. If you
  need an AP to say where a cart is, you have merged layers.
- Layer 2 **never emits facts**. It emits observations *about* facts, with
  noise, latency and gaps.
- Layer 3 **must not import Layer 1**. Enforce it in code: no module under
  `dlai/` may import from `world/`. Add a test that asserts this.
The reason this matters: Layer 1 is the ground truth for evaluating Layer 3.
If Layer 3 can see Layer 1, the blind test is meaningless.
**The identity trap.** In Layer 1 a cart has `cart_id` — a fact. In Layer 2 it
has a MAC — an *observation* that can be randomised, spoofed or shared. The
link between them is an inference, not a field. If `cart_id` and `mac` sit in
the same record, the separation has already collapsed.
---
## 1. Inputs you are given
All under `data/`. Do not regenerate them; they are locked.
| File | Contents |
|---|---|
| `store_layer1.geojson` | 436 entities, locked geometry v1.0 |
| `store_scene3d.json` | extruded solids, openings, sensors, z-extents |
| `centro_scene.stl` | 19,904 triangles, for ray tracing |
| `sensing_layer2.json` | 150 logical sensor positions |
| `cw9172i_rf_table.json` | verified Tx/Rx per MCS per band |
| `material_penetration.json` | ITU-R P.2040-3 attenuation |
| `device_models.json` | cart panel, POS, gates |
| `aireye_emulator_spec.json` | alert taxonomy coverage |
### Coordinate system — do not change
Origin at the SW corner of the leased block. X east, Y north, Z up, metres.
Floor at −6.70 m relative to the complex datum. One clock, UTC, milliseconds.
---
## 2. Locked constants
Anything marked `verified` came from a primary document. Anything marked
`assumed` is an engineering value that must stay overridable at runtime — put
it in config, never inline in a formula.
```yaml
envelope:            # verified — lease + building permit 20140457
  ring: [[0,0],[60,0],[100,25],[100,55],[0,55]]
  area_m2: 5000
  slab_height_m: 6.70
  ceiling_m: 6.40
  floor_level_m: -6.70
sales_floor_m2: 2782         # verified — Yochananof periodic report
back_of_house_m2: 2218       # derived
warehouse_m2: 1747           # derived
x_split_m: 50.58             # derived — solves to sales area
geometry:                    # assumed
  aisles: 10                 # y = 5.40 .. 48.60, pitch 4.80
  aisle_run_m: 34.6
  gondola_depth_m: 1.20
  gondola_height_m: 2.00
  column_grid_m: 8.0
  columns: 77
sensors:
  logical: 150
  units_per_logical: 2       # pair → +2.5 dB selection diversity
  model: CW9172I             # verified datasheet
  mount_z_m: 3.00
  pendant_drop_m: 3.40
  antenna_gain_dbi: {2.4: 4.0, 5: 5.0, 6: 6.0}
  poe: {min_w: 12.95, typical_w: 12.2, max_w: 25.5, class: 802.3at}
heights:
  cart_panel_z_m: 1.00       # the plane that decides everything
  shopper_eye_z_m: 1.60
  gondola_top_z_m: 2.00      # 1.0 m below the sensors — this is the whole problem
attenuation_db:              # ITU-R P.2040-3, per crossing
  2.4: {gondola: 17.7, chiller: 17.7, column: 46.4, cold_room: null}
  5:   {gondola: 33.4, chiller: 33.4, column: 87.7, cold_room: null}
  6:   {gondola: 36.7, chiller: 36.7, column: 96.3, cold_room: null}
  human_body: {2.4: 5, 5: 8}    # assumed, per body crossed
  # cold_room is null = opaque. Metal is a conductor, not a lossy dielectric.
rules:
  location_threshold_dbm: -67   # Cisco Location Deployment Guidelines
  min_aps_for_position: 3
  mount_height_range_m: [2.5, 3.5]
cart_uplink:                 # assumed — no vendor RF figures published
  eirp_dbm: {2.4: 17.0, 5: 16.0, 6: 16.0}
```
### Two facts that drive every design decision
**The link that matters is the uplink.** Location is computed from frames the
sensor *hears from* the cart. The sensor transmits ~24 dBm EIRP; the battery
panel transmits ~16. That 8–14 dB asymmetry is the governing budget. Modelling
the downlink gives an answer that is wrong in the optimistic direction.
**2.4 GHz wins for positioning, decisively.** Full run, 2 m grid, with
first-order reflections:
| band | sales floor ≥3 AP | blind |
|---|---|---|
| 2.4 | **95.5 %** | 1.7 % |
| 5 | 66.0 % | 18.9 % |
| 6 | 64.7 % | 22.8 % |
A gondola costs 17.7 dB at 2.4 and 36.7 at 6. The 2 dB of extra antenna gain
at 6 GHz does not begin to pay for it.
---
## 3. Repository layout
```
world/          Layer 1 — no sensor imports allowed
  geometry.py       load GeoJSON, spatial index, occlusion tests
  entities.py       Cart, Shopper, Checkout, Gate, Dock, Product
  motion.py         waypoint navigation, dwell, queueing
  events.py         pick, pay, exit, undock, dock, fault
  clock.py          single authoritative clock
sensing/        Layer 2 — imports world only to *observe* it
  propagation.py    FSPL + ITU-R P.2040-3 + first-order reflections
  sensor.py         CW9172I model, five radios, duty cycle
  observation.py    Scanning API v3 and Air Marshal record shapes
  pipeline.py       batching, jitter, loss, MAC randomisation
dlai/           Layer 3 — MUST NOT import world/
  ingest.py         consumes sensing output only
  entity.py         entity resolution across observations
  interaction.py    interaction classification
  policy.py         policy engine
  attack.py         attack path, decision
viz/
  server.py         websocket state stream
  twin.html         three.js renderer
eval/
  blind_test.py     compare DLAI output against world ground truth
  metrics.py        precision, recall, position error, flicker
```
---
## 4. Layer 1 — build order
1. **`clock.py` first.** One clock, injected everywhere. Never call
   `time.time()` outside it. Three unsynchronised clocks is the failure mode
   this whole project is built to avoid.
2. **`geometry.py`** — load the GeoJSON, build an R-tree over gondolas,
   columns, cold rooms, chillers. Expose `segment_crossings(a, b)` returning
   counts by material. Everything downstream calls this; make it fast.
3. **`entities.py`** — each entity carries a stable `id` that exists in the
   world, plus a `provenance` field recording how it came to exist.
4. **`motion.py`** — carts follow aisle centrelines with dwell at gondolas;
   shoppers walk more freely. Carts queue at checkout. Speed 0.5–1.2 m/s.
5. **`events.py`** — emit world events with true timestamps: `cart_undocked`,
   `item_picked`, `payment_started`, `cart_exited_gate`.
**Acceptance:** run the world for one simulated hour with sensing disabled.
It must produce a coherent trace. If it cannot, Layer 1 is not independent.
---
## 5. Layer 2 — the sensing model
### Propagation
```
RSSI = EIRP + antenna_gain + diversity − FSPL(d, f) − Σ crossings − body_loss
FSPL = 20·log10(d) + 20·log10(f_MHz) − 27.55
```
First-order reflections by the image method. Mirror the sensor across each
planar surface, trace the image ray, apply Fresnel loss at the incidence
angle, sum powers linearly. Reference implementation in `reflections.py`.
Only evaluate reflections where the direct model already fails the 3-AP rule —
that is the only place a bounce can change the answer, and it makes the full
run tractable. Measured effect: +7 points at 5 GHz, **zero at 2.4**. Steel
gondola backs are excellent reflectors and poor transmitters.
### Human bodies are dynamic absorbers
Shoppers are not decoration. Count the bodies each ray passes through
(shoulder half-width 0.28 m) and charge dB per crossing. This is geometric
blocking that varies with time, not random noise. It produces **flicker** —
carts crossing the 3-AP threshold without moving.
Flicker is the metric that matters. A cart with 8 stable links is fine. A cart
oscillating around 3 produces a position the engine cannot trust, and it will
zigzag in the output while the cart stands still.
### Observation shapes — emit exactly these
**Scanning API v3.** `clientMac`, `ipv4`, `ssid`, `os`, `manufacturer`,
`locations[]` with `x, y, variance, floorPlanId`, `rssiRecords[]` with
`apMac, rssi`.
Note the sign convention differs between `DevicesSeen` (positive) and
`BluetoothDevicesSeen` (negative). Normalise on ingest and record that you did.
**Air Marshal.** `bssid`, `ssid`, `channel`, `firstSeen`, `lastSeen`,
`wiredMacs`, `wiredVlans`, `manufacturer`, `encryption`, `contained`.
`wiredMacs` is the field that separates a rogue attached to your network from
a neighbour. In this building there are five neighbour networks on the same
floor and above it. Without this field the false-positive rate is unusable.
### Model the gaps explicitly
These are not bugs. They are the system, and Layer 3 must be able to tell
"not observed" from "observed as absent".
| Gap | Rule |
|---|---|
| Unassociated randomised MAC | dropped, never reaches the API |
| POST interval | 60–180 s, **not guaranteed** — jitter it |
| Station ↔ station | not reported by any stream |
| Air Marshal containment active | scanning radio time-splits, RTLS accuracy degrades |
| aWIPS syslog | carries **no client identity** at all |
| aWIPS throttling | one message per signature per AP per interval — attack intensity is lost |
| RRM | Tx power moves 2–26 dBm every 30 min |
Every emitted record carries a `gap` field when something was withheld, and a
`confidence` of `observed` or `inferred` — never let an inference claim to be
an observation.
---
## 6. Layer 3 — DLAI
Consumes `sensing/` output only. Enforce with a test.
`ingest → entity resolution → interaction classification → policy → attack path → decision`
Stay in `RECOMMEND_ONLY`. No automated blocking in the PoC.
**Position is a distribution, not a point.** With two sensors you get two
intersection candidates and no way to choose. Resolve by *elimination*: a
candidate inside a gondola or outside the envelope is impossible; a candidate
unreachable from the previous fix at walking speed is impossible. This is what
the geometry buys you — not computing where the cart is, but ruling out where
it cannot be. Carry variance through to the output and never collapse it early.
---
## 7. Visualisation
Three.js, single HTML file, three.js r128 from cdnjs. No `THREE.OrbitControls`
in r128 — write your own. No `CapsuleGeometry` (r142+); use cylinders.
State arrives over websocket at 10 Hz. The renderer holds no simulation logic.
Layer toggles: shell · fixtures · hard occluders · checkout · docks · sensors ·
coverage · blind-only · plan view · links.
**Render the coverage cloud at cart-panel height, 1.00 m** — not at floor
level and not at sensor level. That is the plane where the question is decided,
and the failure only becomes visible when you rotate down to a low angle and
see the sensor at 3.00 m looking over a gondola at 2.00 m.
Draw a line from each cart to every sensor currently hearing it, coloured by
RSSI. A cart with fewer than three turns red. That single behaviour is the
thesis of the whole build: this is a positioning failure, not a coverage one.
---
## 8. The blind test — the point of all of it
`eval/blind_test.py`
1. Run the world for N hours. Record ground truth.
2. Feed only Layer 2 observations to Layer 3.
3. Compare: position error, detection precision/recall, time-to-detect,
   false-positive rate by zone.
4. Report per zone. **Back of house will be worst** — 28 sensors over 2,218 m²
   gives ~80 % and 19.3 % blind in every band, and no physics fixes it. Only
   reallocation does.
This test does not need the live branch. It can run today.
---
## 9. Known-open — mark these in code, do not paper over them
- Interior fit-out is `assumed`. Gondola and checkout positions are a
  parametric model, not a survey.
- Gondola attenuation 17.7/33.4/36.7 dB is computed from ITU-R P.2040-3 with
  an equivalent-thickness assumption. **It is the most sensitive parameter in
  the model.** A loaded shelf is a steel back panel plus mixed goods, not a
  homogeneous dielectric. First thing to measure.
- Cart panel Tx power is a class value. No vendor figure exists.
- Inside/outside discrimination does not work. A modem 8 m out in the
  courtyard is heard by 41 sensors at −44 dBm. The 18 perimeter units do not
  separate anything. Do not build a feature that assumes they do.
- Back-of-house density is under-provisioned by design of the v0.2 quotas.
---
## 10. Style
Python 3.11+, type hints, dataclasses for entities, `pytest`. Physics in pure
functions with no hidden state. Every constant that is `assumed` lives in
config and is overridable at runtime. Every module docstring states which
layer it belongs to and what it may import.
Log provenance on every derived value. When someone asks in six months why a
number is what it is, the answer must be in the code.
