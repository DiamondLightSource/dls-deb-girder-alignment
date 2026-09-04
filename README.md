# Girder Alignment Tool — V2

Build-bay alignment for accelerator girders. Takes a laser-tracker survey, computes the
correction, and walks a technician through the jack and stage moves with live encoder
readback, tolerance gating and a signed PDF record.

Supports all four girder fabrications: **MS, LM, ML, SM**.

---

## Quick start

```bash
./run.sh --demo --open        # simulator: training / off-site, no EPICS needed
./run.sh --open               # live EPICS
```
Windows: `run.bat --demo --open`

First run creates a virtual environment and installs dependencies. Then open
<http://localhost:8080/>.

---

## Why there is a backend

A browser **cannot reach EPICS**. Channel Access is raw UDP/TCP on ports 5064/5065 and
JavaScript has no UDP socket access, so a local Python service is required. Once it
exists, session persistence, PDF generation and a single authoritative maths
implementation all follow.

**The alignment maths lives in `geometry.py` and is authoritative.** The browser is a
view. Do not re-implement the numerics in JavaScript — two copies will drift, and a
silent numerical discrepancy in this domain is expensive.

**EPICS access is read-only.** The tool never writes to an IOC; adjustment stays manual.
Do not add a `caput` path.

---

## Configuration

### `config.yaml`
Set the encoder PV names, the archiver host, and the gate default.

**Each build bay has its own encoder rig, so its own PV set.** `epics.bay_pv_map` holds
one block per bay; the bay chosen at session start selects the block, and bays not listed
fall back to `epics.pv_map`. Switching bay also drops any display zero, since the datum
belonged to the previous bay's encoders.

```yaml
epics:
  live_backend: auto            # auto | cothread | pyepics | aa.ca | demo
  archiver_host: archiver.diamond.ac.uk
  pv_map:
    V_US_IN:  "BL-GIR-ENC-01:POS"
    ...
gate:
  default_mm: 0.010
  encoder_resolution_mm: 0.001  # the gate is never allowed finer than this
```

The eight encoder ids are fixed by the geometry model:

| Vertical | Stage |
|---|---|
| `V_US_IN`, `V_US_OUT`, `V_DS_IN`, `V_DS_OUT` | `SURGE_US`, `SWAY_US`, `SURGE_DS`, `SWAY_DS` |

### Clearing the session database

Yes, `sessions/sessions.sqlite` is a single file and deleting it is safe — the server
recreates an empty one on next start. But it discards the audit trail for every girder
ever aligned, so `manage_db.py` offers narrower options and archives before anything
destructive:

```bash
python manage_db.py list                     # what is in there
python manage_db.py stats                    # size and counts
python manage_db.py export                   # dump every session to JSON first
python manage_db.py delete <id> [<id> ...]   # specific sessions
python manage_db.py purge --test             # drop rows whose serial is not a DLS number
python manage_db.py purge --before 2026-01-01
python manage_db.py clear                    # empty it (archives first)
python manage_db.py vacuum                   # reclaim space after deleting
```

Reports already written to disk are never touched — they live in `reports/`, not in the
database. `purge --test` is the usual one after commissioning: it removes anything whose
serial does not start `DLS`, i.e. the demo and trial sessions.

### `girder_serials.csv`
Serials do **not** encode girder type, so this table is the authority:

```csv
serial,type,notes
SR-GIR-0142,MS,
```

An unknown serial is not an error — the operator selects the type manually and is
prompted to add the row.

---

## Dependencies

Core: `flask`, `numpy`, `reportlab`, `PyYAML` (installed by the launcher).

For **live** EPICS, install **one** of:

```bash
pip install pyepics       # Windows and Linux — use this on Windows
pip install cothread      # Linux only
```

**On Windows, use pyepics.** cothread has a C extension (`cothread._coroutine`) and
publishes no Windows wheel, so pip falls back to building from source and fails with
*"Microsoft Visual C++ 14.0 or greater is required"*. You do not need cothread — the
backend order is cothread → pyepics → aa.ca, so with cothread absent the tool picks up
pyepics on its own. (If you specifically want cothread on Windows you would have to
install the Visual C++ Build Tools first, but there is no benefit here.)

### Two common Windows problems

**`aa.ca not installed (Descriptors cannot be created directly)`** and archiver errors —
aapy's generated protobuf code needs protobuf 3.20.x, and pip installs 7.x:

```bash
pip install "protobuf<3.21"
```

**`Failed to start executable - "caRepeater"`** — harmless. caRepeater is a helper daemon,
not needed for reads. It is not the reason PVs fail to connect.

**Some PVs work with caget, others do not.** The archiver is irrelevant here — it is an
HTTP service holding history, while Channel Access talks straight to the IOC. A PV being
archived says nothing about whether you can reach its IOC live.

CA finds a PV by broadcasting a search on UDP 5064. With `EPICS_CA_AUTO_ADDR_LIST=YES`
(the default) it broadcasts only on the PC's **own** subnets, so an IOC on a different
subnet never sees the search and the PV looks simply "not found" — indistinguishable from
a typo. Live machine PVs usually resolve because Diamond runs **CA gateways** proxying the
accelerator networks to the office network; a gateway only serves the PV patterns it is
configured for, so a rig IOC on its own subnet is not covered.

Prove it by asking a PV that *does* work which host serves it, and compare:

```bash
python check_epics.py --probe TS01C-AL-GIRDR-01:US:Y_IB <a-machine-PV-that-works>
```

A resolving PV prints the IOC serving it. If that host contains **`gate`** (e.g.
`cs03r-cs-gate-01.diamond.ac.uk:5064`) then your PC has **no direct CA route at all** — it
only sees what that CA gateway proxies, and the gateway does not cover the girder rig.

Before going to Controls, rule out a wrong PV name using the archiver, which is reachable
over plain HTTP even when CA is blocked:

```bash
python check_epics.py --find "TS01C-AL-GIRDR-01*"
```

If the names come back, they are real and archived, so the CA failure is purely routing.
If nothing matches, either the prefix is wrong or those PVs are not archived — and in that
case the `live_backend: archiver` fallback will not work for them either.

Then ask Controls either for a gateway entry covering the girder rig, or for its IOC
hostname so you can add it:

```bash
# Windows                                   # Linux
set EPICS_CA_ADDR_LIST=<ioc-host-or-bcast>  export EPICS_CA_ADDR_LIST=<ioc-host-or-bcast>
```

Leave `EPICS_CA_AUTO_ADDR_LIST=YES` and just add the extra address — setting it to `NO`
restricts CA to your list only and will break the PVs that currently work.

**PVs show NOT CONNECTED but the archiver works.** That combination means the PC can reach
the archiver but not the IOCs — normal for an office machine outside the controls network.
Channel Access needs UDP/TCP 5064-5065 to the IOC subnet. Fixes, best first:

1. Set `EPICS_CA_ADDR_LIST` to the IOC host or its broadcast address, and have the
   firewall allow those ports.
2. Run the tool on a PC that already has CA access.
3. Temporary fallback: `live_backend: archiver` in `config.yaml`. Values then come from
   the Archiver Appliance instead of Channel Access. **Adequate for looking around, not
   for following a move** — archived data is deadbanded, decimated and seconds late, so a
   jack you are turning will appear to lag or not move at all. It is never selected
   automatically; you have to ask for it.

Check everything is reachable before the first live alignment:

```bash
python check_epics.py             # backends, every PV, temperature, archiver
python check_epics.py --bay 1
python check_epics.py --watch     # keep polling, to watch an encoder move
```

`--watch` is the quickest way to confirm the sign conventions on a new rig: raise a jack
and check its vertical encoder counts **up** (−Y = +count), and drive both ends in +Z to
confirm `:DS:Z` reads **negative** while `:US:Z` reads positive.

For **archiver traces** (Diamond):

```bash
pip install aapy          # installs as aapy, imports as aa
```

`aapy` is only used for historical traces in the report. Live readings never come from
the archiver — it is deadbanded and decimated, so a technician following a move would be
watching stale values.

If a live backend is missing the server falls back to the simulator and says so.

---

## Workflow

1. **Start session** — girder serial (type resolves from the lookup), operator name,
   vertical mode, pairing, and the on-target gate.
2. **Load survey** — CSV of measured points, `X,Y,Z` per line; a name/index column is
   ignored. Points are matched to reference fiducials **by proximity** (10 mm gate), not
   by name, because names repeat between girder types.
3. **Move**, step by step. Each step shows the two active encoders large, with target,
   error and direction, plus monitors underneath.
4. **Confirm step** — blocked unless every active encoder is inside the gate, the
   parasitic monitors are in limit and no reading is stale. Override is possible and is
   recorded with name and reason.
5. **Re-survey** and iterate until every axis is in tolerance.
6. **Generate report** — PDF plus a JSON sibling in `reports/`.
7. **Start next girder** — you are prompted to generate the report first if none exists
   for that girder. Once a session is complete or stopped, the survey and move
   controls lock and a **Start next girder** button appears. It frees the bay and returns
   to the setup screen with the serial cleared (the operator name is kept, since it is
   usually the same person). Nothing is discarded: the session is already saved and can
   be reopened from *Resume previous*.

### Adjustment order
Sequencing follows the **coupling**, not the tolerance priority: vertical first (roll
creeps X/Y through the geometric lever), horizontal last (nearly one-way coupled, so it
absorbs the creep without re-breaking the vertical).

**Sway and surge are separate steps and are fully independent.** They act along
perpendicular stage axes, and because a stage only ever translates, yaw — which comes
purely from differential sway — produces no surge-encoder motion. Both cross-terms are
zero on the real geometry, so both steps use final targets and the order between them is
free. Sway is listed first only because it absorbs the roll-induced lateral creep left by
the vertical stage.

**Why the two encoder families need different levers.** Vertical encoders contact the
girder underside, so they ride the rigid body and their own position is the lever — that
is what produces the ~0.049 mm/mm cross-shift between jack pairs. Surge/sway encoders read
*stage* translation, and a stage never rotates, so the reading is the same wherever the
encoder is mounted; the lever is the stage's location on the girder axis (the bearing-pair
midpoint), **not** the encoder position. Using the encoder position instead injects its
transverse offset and invents a surge/yaw coupling that does not exist.

### Room temperature
Each bay averages the nearest sensors, and the subsets overlap
(bay 1 = 1,2,3 · bay 2 = 2,3,4,5 · bay 3 = 4,5,6). The operator picks the bay at session
start and the survey temperature is read from those PVs automatically; a manual entry
overrides it. Disconnected sensors are excluded from the mean rather than poisoning it,
and the count actually used is recorded. Sensor PVs are `TS01C-EA-GIRDR-01:TMON01`..`TMON07`.

The feed's health is shown live and updated every 5 s: a **Bay temp** readout in the
header outlined green/amber/red, and a line under the survey upload saying how many
sensors reported. A dead sensor is called out by name, and a spread over 0.5 °C is
flagged since the room is held to ±0.5 °C. This matters because a stale reading is
indistinguishable from a steady one unless the tool says so.

### Wrong survey file
*"↩ Wrong file"* discards the last survey and reopens the upload — available even when
the fit came back in tolerance, since that is exactly when a wrong file is most likely to
be noticed. Nothing has moved, so this is a data correction, not a machine action; moves
already made stay in the log so the record remains truthful.

### Visualisation
A **Visualise moves** panel offers a plan + end view (SVG, always available) and an
optional 3D model. Both are ported from the V1-2 viewer so they look and behave the same,
and both draw the correction vectors from the server's solved pose — the browser never
recomputes the maths. Plan and end views are drawn in the **jack frame**: the plan view
puts the girder horizontal, the end view looks downstream.

The 3D view works in master mm coordinates and uses **Creo Parametric's default mouse
mapping**, so it behaves like the CAD the engineers already use:

| action | control |
|---|---|
| spin / rotate | middle drag |
| pan | shift + middle drag |
| zoom | ctrl + middle drag, or scroll wheel |

Left drag also spins, for anyone without a middle button. There is a Δ-scale slider for
the correction vectors and a model-opacity slider (100% is fully opaque — the material
switches out of transparent mode entirely at the top of the range, so there is no residual
see-through). It tries **OBJ + MTL first** (materials
come with it) and falls back to **STL**, then to a plain envelope box if neither is
present. Put `<type>_bare_girder.obj` / `.mtl` / `.stl` in `static/`. three.js loads from
a CDN, so the 3D view needs internet on first use; the plan and end views are pure SVG
and work offline.

**Both 2D views are also drawn into the PDF**, using ReportLab's own primitives rather
than matplotlib — an optional import that fails silently is exactly how these views went
missing from the report before, so the report now has no dependency that can vanish.

### Sequential pairs and intermediate targets
Each vertical encoder sits ~325 mm along-axis from its own bearing, so moving one pair
re-tilts the girder about the other and shifts the un-moved pair's reading by
**≈0.049 mm per mm** of far-pair travel.

**Only the last pair moved lands on final targets.** The first pair stops at a
compensated intermediate value, shown alongside the value it settles to. This applies to
*any* sequential pair move — flatten, heave, or the two combined. It vanishes only for a
simultaneous four-jack move, which is impractical by hand.

### Combined vertical
Roll, pitch and heave are all combinations of the same four jack heights, so they can be
delivered in one jack pass. The combined targets equal flatten + heave exactly. Useful
for fine iterations; the discrete flatten is still worth using on a first alignment of
~5 mm, where seating the bearings before loading a large heave matters and the
bearing-ride risk is highest.

---

## Safety behaviours

- **Zeroing is a display datum only**, never a hardware zero. Absolute values stay
  visible because they are what prove the ±6 mm travel budget.
- **Stale readings**: a frozen PV looks exactly like a stationary girder, so every
  reading carries its age and goes visibly stale rather than showing the last good value.
- **Stage encoders are monitored during vertical moves.** They should barely move; if
  they do, the girder is riding the spherical bearing.
- **Parasitic modes** WARP (vertical twist) and STRETCH (differential surge) are the two
  non-rigid encoder combinations. Warn 0.003 mm, stop 0.005 mm. The gate tests these too
  — on target with WARP out of limit is not a step to wave through.
- **Expected values** are shown for monitored encoders, so "moved as expected because the
  girder tilted" is distinguishable from "moved unexpectedly".
- **Travel projection** warns before cranking, not when the jack runs out.
- **Step back is not undo.** The UI can rewind; the steel cannot. Re-opening a step never
  replays stale targets. If the pose has drifted, *Abandon iteration* and re-survey — that
  is the honest recovery.

---

## Reports

Written to `reports/` as a PDF plus a JSON sibling. No asset-DB API yet, so they queue on
disk; the JSON is what the database should ingest when an API exists.

Contents: serial, type, operator, geometry revision, software version, temperature,
survey history with fit quality, initial vs as-left pose, target vs achieved per move,
parasitic extremes, overrides with reasons, model validation, the uploaded survey data,
and a sign-off block.

Stopping mid-session can save a **partial** report, watermarked INCOMPLETE so it can
never be mistaken for a finished alignment.

---

## Geometry

`_geom_data.py` is generated from the reference-point workbook and carries a revision
string recorded on every report. Current revision: **2026-09-02-r2**, which includes
three corrections found during data review:

- LM/ML/SM point lists carried MS's rotation; re-derived through each girder's own
  transform and confirmed in CAD (shifts up to 163 mm).
- ML fiducials were missing the +25 mm puck offset (Y −475 → −450), which would have
  biased ML heave by 25 mm.
- Downstream inboard/outboard labels were swapped. Labels are now **derived from
  position**, so they cannot recur.

Inboard/outboard and upstream/downstream are computed from each point's position in the
jack frame — never hand-labelled.

---

## Layout

```
server.py           Flask service and REST API
geometry.py         AUTHORITATIVE maths: frames, Jacobian, pose fit, planner
_geom_data.py       generated geometry for MS/LM/ML/SM
epics_io.py         live CA backends + aapy archiver; read-only
session.py          state machine, SQLite persistence, gating, audit trail
report.py           PDF + JSON generation
static/index.html   the UI
config.yaml         PV map, archiver, gate defaults
girder_serials.csv  serial -> girder type lookup
reports/            generated reports
sessions/           SQLite session store
```

---

## Testing without hardware

`--demo` simulates a rigid girder: the encoders respond to a commanded pose the way real
ones would, including the cross-shift on the un-moved pair. That makes it usable as a
**training mode**, not just a development convenience. *Simulate move* drives the
encoders onto the current step's expected state.
