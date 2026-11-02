[![CI](https://github.com/DiamondLightSource/dls-deb-girder-alignment/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-deb-girder-alignment/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-deb-girder-alignment/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-deb-girder-alignment)
[![PyPI](https://img.shields.io/pypi/v/dls-deb-girder-alignment.svg)](https://pypi.org/project/dls-deb-girder-alignment)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# Girder Alignment

Build-bay alignment for Diamond-II accelerator girders.

Take a laser-tracker survey of a girder sitting on its jacks, and this works out
the correction and walks a technician through the jack and stage moves — live
encoder readback, a tolerance gate on every step, and a signed PDF record at the
end. Supports all four fabrications: **MS, LM, ML, SM**.

It runs as a web service, one deployment per build bay, alongside the IOCs that
serve the encoder PVs.

---

## Try it in two minutes

No EPICS needed — `--demo` runs a simulator good enough to rehearse the whole
procedure.

```bash
uv sync
uv run dls-deb-girder-alignment serve --demo --config example/config/config.yaml
```

Open <http://localhost:8080/> and:

1. Serial `DLS0011116`, any operator name, **Start session** (Bare girder and
   Combined vertical are on by default — keep them).
2. Load `example/surveys/ms_DLS0011116_initial.csv`. The fit comes back at about
   0.02 mm RMS with every axis out of tolerance — review the pose error, then
   **Accept survey — generate correction steps** to build the four-step plan.
3. For each step: **Simulate move**, wait for the gate to go green, then
   **Confirm step complete**.
4. Load `example/surveys/ms_DLS0011116_as_left.csv`. Every axis is now in
   tolerance and the session completes.
5. **Generate report** — a PDF and its JSON sibling.

If port 8080 is taken, add `--port 8081`.

The example surveys place the girder at a *known* pose, written into each file's
header, so you can check what the tool fits back out. See
[`example/surveys/`](example/surveys/) for the full set and how to make more.

---

## Running for real

```bash
dls-deb-girder-alignment serve --config /path/to/config.yaml
```

Drop `--demo` and it talks Channel Access through `cothread`. Before the first
live alignment, prove the PVs are reachable:

```bash
dls-deb-girder-alignment check              # every encoder, plus temperature
dls-deb-girder-alignment check --watch      # keep polling, to watch a jack move
```

`check` exits non-zero if any encoder is unreachable, so it works as a smoke test.

> **If you asked for live mode and the banner says DEMO**, Channel Access failed
> to start and the service fell back to the simulator. It says so on stderr.

---

## Commands

| | |
|---|---|
| `serve` | run the web service |
| `check` | check Channel Access to this bay's PVs |
| `sessions list` | what is in the session store |
| `sessions export --out FILE` | dump every session as JSON |

Common options: `--config`, `--serials`, `--domain`, `--demo`.
`serve` adds `--host`, `--port` and `--dev` (Flask's development server instead
of waitress). `--help` on any of them for the details.

`scripts/mirror_reports.py` is not part of the package — it is a standalone,
stdlib-only script that copies reports off a running deployment onto backed-up
storage, over the same HTTP endpoints the web UI uses. Run it from cron on any
machine that can reach the service and has the share mounted:

```bash
scripts/mirror_reports.py --url https://deb-girder-bay-01.diamond.ac.uk \
                          --dest /dls/sdrive/<group>/girder-alignment/bay1 --sessions
```

It adds missing reports and never deletes them. **Reports on the
deployment's PersistentVolumeClaim are not backed up**; see the header of the
script for why the mirror is pulled from outside rather than written from the
pod.

`--sessions` additionally saves the whole session store as
`sessions_<YYYYmmdd>.json`, fetched from `/api/sessions/export`. Every report
already carries its own session in the `.json` beside the PDF — the same
content the database holds — so this covers only the sessions that never
produced a report: abandoned partway, or open when the volume is lost. It is
pulled as JSON rather than by copying `sessions.sqlite`, because a file copy
taken while the service is writing can be torn.

`scripts/calibrate.py` is the same kind of standalone, stdlib-only script,
for a different job: it pools `model_check` (predicted-vs-achieved pose
change) across many stored sessions and looks for a systematic slope away
from 1.0, per girder type and axis — including `warp`, where a persistent
slope is exactly the signal that `_fea_data.FEA_COMPLIANCE`'s stored
compliance gain should be rescaled. It reads whatever `sessions export`
already produces, so the two pair naturally:

```bash
uv run dls-deb-girder-alignment sessions export --out export.json
scripts/calibrate.py export.json
```

It only ever *suggests* — nothing it finds is applied automatically, and a
slope with too few samples or too weak a correlation is flagged rather than
acted on. See the script's own header for the full methodology and its
limits (it finds a uniform per-type scale correction, not a per-point one).

### Containerised report mirror

Run the mirror on an always-on machine that can reach the web service and has
the backed-up group share mounted with write access. It needs no Channel Access
connection or access to the application's PVC. Running the script directly
requires Python 3.11 or newer; the separate mirror image includes Python and
does not install the web application.

Build from this repository's root:

```bash
docker build -f Dockerfile.mirror -t girder-report-mirror .
```

`Dockerfile.mirror` is the separate build recipe for this image.
`Dockerfile.mirror.dockerignore` filters its build context, like `.gitignore`
filters files for Git: only the mirror script is included. The release workflow
publishes `ghcr.io/diamondlightsource/dls-deb-girder-alignment-mirror:<tag>`
alongside the web application's image, using the same release tag.

Create a directory on the share, replacing `<group>` with your actual group.
Use a separate destination for each bay because report names do not include
the bay. Run as an account with permission to write there:

```bash
mkdir -p /dls/sdrive/<group>/girder-alignment/bay1
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src=/dls/sdrive/<group>/girder-alignment/bay1,dst=/backup \
  girder-report-mirror \
  --url https://deb-girder-bay-01.diamond.ac.uk \
  --dest /backup --sessions
```

If share access requires a supplementary group, also pass its numeric GID with
`--group-add`. Add `--dry-run` to check the report listing without copying;
this does not test destination writes or download the session export.

The container runs once and exits. For example, save the `docker run` command
above in an executable `/home/mirror/bin/mirror-bay1`, then add this to that
account's crontab (adjust the paths to match the account):

```cron
*/15 * * * * /usr/bin/flock -n /home/mirror/mirror-bay1.lock /home/mirror/bin/mirror-bay1 >> /home/mirror/mirror-bay1.log 2>&1
```

The wrapper should start with `#!/bin/sh` and use the absolute path to Docker
on that host. Ensure the share is mounted before running it, and monitor the
log for failed runs. All runs for the same destination, including manual runs,
must use the same lock: the script's `.part` filenames are shared. To run the
wrapper manually with the lock:

```bash
/usr/bin/flock -n /home/mirror/mirror-bay1.lock /home/mirror/bin/mirror-bay1
```

A Kubernetes CronJob is also possible with `concurrencyPolicy: Forbid`, but it
must mount the backed-up destination. The accelerator IOC nodes do not mount
`/dls`; containerising the mirror does not provide that mount. Another PVC in
the same cluster is not a substitute for backed-up storage.

---

## Configuration

**No site configuration ships with this package.** Real PV names, the bay's
domain and the girder serial table are deployment data — they change without a
release and they differ per bay — so the application is given a path and reads
what it finds there.

The config file is found in this order:

1. `--config <path>` on the command line
2. `$GIRDER_CONFIG`
3. `/epics/ioc/config/config.yaml`

The third is where a DLS `*-services` repo mounts a service's `config/` directory
as a ConfigMap, so **a deployed container needs no arguments at all**.
`girder_serials.csv` is looked for beside the config file, so both travel
together in the same ConfigMap.

[`example/config/`](example/config/) is the template to copy into a service
directory. The test suite loads it, so it cannot rot.

```
services/ts01c-girder-align/
├── Chart.yaml
├── config/
│   ├── config.yaml           <- copied from example/config/
│   └── girder_serials.csv
└── values.yaml
```

### Where reports are saved

Three places set it. The most specific one wins:

| | where | scope |
|---|---|---|
| 1 | the **Folder** box on the session card in the UI | one report |
| 2 | `$GIRDER_REPORTS` | this deployment |
| 3 | `paths.reports` in `config.yaml` | this deployment |

Leave the Folder box empty to use the configured default.

The directory is created if it does not exist and is write-tested *before* the
report is built, so a bad path fails immediately with a message rather than after
the work. Files are named:

```
girder_<serial>_<type>_<YYYYmmdd-HHMMSS>_FINAL.pdf     + .json sibling
girder_<serial>_<type>_<YYYYmmdd-HHMMSS>_PARTIAL.pdf   stopped mid-alignment
```

A **PARTIAL** report is watermarked INCOMPLETE so it can never be mistaken for a
finished alignment.

> **One wrinkle**: only reports written to the configured default are
> downloadable through the browser at `/reports/<name>`. A custom folder is
> written but not served — the response says so with `"servable": false`.

### Per bay

One deployment serves one bay, and the same image serves them all. Change the
domain and the sensor list:

```yaml
epics:
  domain: TS01C          # TS02C for bay 2, TS03C for bay 3

temperature:
  sensor_pvs:            # full PV names - these do NOT follow epics.domain
    - "TS01C-EA-GIRDR-01:TMON01"
    - "TS01C-EA-GIRDR-01:TMON02"
    - "TS01C-EA-GIRDR-01:TMON03"
```

Every encoder PV is built from `epics.domain`
(`TS01C-AL-GIRDR-01:US:Y_IB` and so on). The **temperature sensors are not** —
they are one array covering the whole hall, all under `TS01C`, and the bays map
onto overlapping subsets of it (bay 1 = sensors 1–3, bay 2 = 2–5, bay 3 = 4–6).

### Channel Access

CA settings go under `epics.ca` in the config file and are put into the
environment before the CA library loads.

```yaml
epics:
  ca:
    server_port: 6064            # the DEB build area, not the 5064 default
    # repeater_port: 6065        # derived as server_port + 1 when omitted
    # auto_address_list: "NO"    # switch the broadcast search off
    # name_servers: "deb-epics-gateways:5064"   # ... and go via the CA gateway
    # address_list: "172.23.x.x"                # ... or name the IOC directly
```

| key | variable | notes |
|---|---|---|
| `server_port` | `EPICS_CA_SERVER_PORT` | the port the **IOCs** serve on |
| `repeater_port` | `EPICS_CA_REPEATER_PORT` | defaults to `server_port + 1` |
| `auto_address_list` | `EPICS_CA_AUTO_ADDR_LIST` | `"NO"` disables broadcast search |
| `address_list` | `EPICS_CA_ADDR_LIST` | explicit search addresses |
| `name_servers` | `EPICS_CA_NAME_SERVERS` | search over TCP via a gateway |
| `connection_timeout` | `EPICS_CA_CONN_TMO` | |
| `max_array_bytes` | `EPICS_CA_MAX_ARRAY_BYTES` | |

Any other setting can be given by its full `EPICS_*` name instead of a key from
this table.

Two things are easy to get wrong here:

- **The repeater port does not follow the server port by itself.** EPICS Base
  defaults `EPICS_CA_REPEATER_PORT` to a flat 5065 whatever the server port is.
  Setting `server_port` alone derives 6065 for you, which is what DLS
  `. changeports 6064` does by hand — so leave `repeater_port` out unless you
  really do mean something else.
- **A broadcast search only works where broadcasts reach the IOCs**: a
  workstation on the machine network, or a pod using `hostNetwork`. A pod on
  the ordinary cluster network needs `auto_address_list: "NO"` plus either
  `name_servers` pointing at the namespace's CA gateway or an explicit
  `address_list`.

An `EPICS_CA_*` variable already set in the environment always wins, so a
deployment can repoint the tool without editing the config.

### Environment variables

All override the config file, for deployment:

| variable | effect |
|---|---|
| `GIRDER_CONFIG` | path to `config.yaml` |
| `GIRDER_DOMAIN` | this bay's encoder domain, e.g. `TS02C` |
| `GIRDER_BAY` | bay label printed on the report |
| `GIRDER_REPORTS` | where reports are written |
| `GIRDER_SESSIONS_DB` | path to the SQLite session store |
| `GIRDER_MODELS` | directory holding the optional 3D girder models |

### 3D models

`<type>_bare_girder.obj` / `.mtl` / `.stl` come to about 79 MB, so they are not
in the package or the container image. Point `$GIRDER_MODELS` at a mounted
directory to switch the 3D view on. Without it the plan and end views — pure SVG,
and the ones that go into the PDF — work as normal, and the 3D view falls back to
a plain envelope box.

---

## How it works

### Why there is a backend

A browser cannot reach EPICS: Channel Access is raw UDP/TCP on ports 5064/5065
and JavaScript has no UDP socket. Once a service exists, session persistence, PDF
generation and a single authoritative maths implementation all follow.

**The alignment maths lives in `geometry.py` and is authoritative.** The browser
is a view. Do not re-implement the numerics in JavaScript — two copies will
drift, and a silent numerical discrepancy in this domain is expensive.

**The tool never commands motion, and has no write path at all.** Adjustment
stays manual and the tool only observes. Encoder zeroing (setting a hardware
datum in the IOC) is done directly in EPICS, outside this tool.

### The survey

Points are matched to reference fiducials **by position** (10 mm gate), never by
name: names repeat between girder types, so matching on position is what makes an
uploaded survey safe to bind to a girder. A weighted small-angle least-squares
rigid fit then gives the 6-DOF pose error in the girder's own jack frame.

| axis | tolerance | | axis | tolerance |
|---|---|---|---|---|
| roll | 0.05 mrad | | yaw | 0.15 mrad |
| sway | 0.10 mm | | pitch | 0.15 mrad |
| heave | 0.10 mm | | surge | 0.20 mm |

### The move plan

Sequencing follows the **coupling**, not the tolerance priority: vertical first
(roll creeps X/Y through the geometric lever), horizontal last (nearly one-way
coupled, so it absorbs the creep without re-breaking the vertical).

**Only the last pair moved lands on final targets.** Each vertical encoder sits
~325 mm along-axis from its own bearing, so moving one jack pair re-tilts the
girder about the other and shifts the un-moved pair's reading by ≈0.049 mm per mm
of far-pair travel. The first pair therefore stops at a compensated intermediate
value, shown alongside the value it will settle to.

**Sway and surge are independent.** They act along perpendicular stage axes, and
because a stage only translates, yaw — which comes purely from differential sway
— produces no surge-encoder motion at all.

**The two encoder families need different levers.** Vertical encoders contact the
girder underside, so they ride the rigid body and their own position is the
lever — that is what produces the cross-shift above. Surge/sway encoders read
*stage* translation, and a stage never rotates, so the lever is the stage's
location on the girder axis (the bearing-pair midpoint), **not** the encoder
position. Using the encoder position instead injects its transverse offset and
invents a surge/yaw coupling that does not exist.

### Two datums, and which is which

Worth understanding before changing anything in `session.py`.

**The hardware datum lives in the IOC.** Zeroing an encoder — setting `…:X_SP`
to zero and processing `…:X_ZCALC.PROC`, so the IOC computes an offset that
autosave persists — is done directly in EPICS, outside this tool. This tool
only observes the result: a plan datum captured before an external zero is
meaningless, since the absolute readings have just moved (see below).

**The plan datum lives in the session.** Step targets are encoder *deltas* from
the pose that was surveyed, so a reading only means something measured from the
value the encoder had when its move group opened. Move groups are `flatten`,
`heave` (or `combined`) and `horizontal`; heave targets assume flatten has
already been applied, so the reference has to be re-taken at each boundary or the
gate compares against the wrong thing.

The plan datum is captured **automatically** on entering a group and is part of
the persisted session, so a restart mid-move resumes against the same reference
rather than silently changing it.

> **Zeroing an encoder externally, mid-move, leaves a stale datum.** Because
> zeroing happens outside this tool now, the tool has no way to know it
> happened and will not drop the datum on its own — unlike a move or a new
> survey, nothing here currently re-takes it automatically. If an encoder is
> zeroed while a move is in progress, re-open the current step (or start a
> fresh survey) before continuing, so the datum is re-taken against the new
> absolute readings.

### Heave pre-set (one shared value, all four types)

This tool normally aligns the **bare** girder, under its own weight, before
any magnets are mounted. Magnets are then mounted afterwards and aligned
individually, to nominal, **after** the girder is fully loaded — so a
magnet's own fine adjuster, not this tool, is what brings it onto the beam
trajectory. Between bare and fully-loaded the girder sags under the magnets'
weight (`_fea_data.SAG_BIAS_MM`): in MS's FEA run, ~-0.535 mm at the centre,
~+0.056 mm at the D/S end. That sag doesn't change where any magnet ends up
— but it does change how much travel each magnet's adjuster has to use, and
a bigger ask is a bigger chance of running out of range on the worst-placed
magnet.

So the bare-girder target is deliberately offset **above** true nominal by a
constant (`Machine.sag_bias_mm` — the exact minimax split of the sag
extremes above, which equalises the worst-case magnet correction at both the
centre and the ends rather than leaving it all at the centre). **+0.24 mm,
from MS's bare-vs-loaded FEA run, is applied to all four girder types** —
LM had its own run too (+0.113 mm) but that per-type approach was dropped in
favour of this single shared value (2026-10-12).

Since the bias only makes sense while the girder is still bare, **the
"Start alignment session" form has a Girder state toggle** — *Bare girder*
(default) or *Loaded with magnets*. Bare uses the pre-set exactly as
above; Loaded sends `heave_bias_mm: 0.0` explicitly, so a girder that has
already sagged under its real magnet load isn't pre-set on top of that
real sag. It defaults on for a bare girder, folded into the same "heave" or
"combined" step that already sets heave — nothing extra for the technician
to do beyond that one toggle — and is recorded on every survey so it's
never mistaken for a mis-alignment. `compute_plan`'s `heave_bias_mm` and
`Session`'s constructor both accept an explicit override too, for any other
case the toggle doesn't cover.

### Safety behaviours

- **Parasitic modes.** Eight encoders, six degrees of freedom — so two encoder
  combinations exist that a *rigid* girder cannot produce. They are named WARP
  (vertical twist) and STRETCH (differential surge), and they are the left null
  space of the Jacobian. Warn at 0.003 mm, stop at 0.005 mm. The gate tests them
  too: on target with WARP out of limit is not a step to wave through — but it
  gates on the STEP'S OWN expected parasitic content, not on zero, since a
  warp-corrected heave move (below) or even an ordinary intermediate pair-move
  deliberately/genuinely has some.
- **Warp correction (all four types).** The four vertical jacks give exactly
  three rigid DOF (heave, pitch, roll); the fourth jack combination — the
  diagonal checkerboard, geometrically the same null space as WARP above — is
  not rigid. Preliminary single-jack FEA shows the girder genuinely bends in
  that mode (`_fea_data.py`, ~0.7-1.0 mm of real shape change at the survey
  points per mm of checkerboard jack command). MS and LM were each run through
  their own FEA sweep; SM reuses MS's numbers and ML reuses LM's, since each
  pair shares an identical jack/vencoder/fiducial layout in the jack frame
  (only the frame's own offset into the master frame differs, which this tool
  already accounts for elsewhere). `fit_pose` fits this as a 7th parameter
  (6 rigid DOF + warp amplitude) whenever a girder type has FEA data for enough
  matched fiducials, and the correction defaults ON, folded into whichever
  vertical step actually sets heave — "Heave" alone or "Vertical (flatten +
  heave)" when combined — never "Flatten" alone. Horizontal (surge/sway)
  actuation has negligible effect on this mode and is not modelled; STRETCH is
  corrected via the surge adjusters, unaffected by this. A girder type absent
  from `FEA_COMPLIANCE` (none currently) would behave exactly as before —
  `err["warp"]` unmodelled (0.0), not assumed zero.
- **Connection, not freshness.** Every reading carries the age of its
  timestamp, but the gate never judges it. These encoder records only process
  when the PLC value changes, so a girder that has stopped moving carries an
  old timestamp on all eight — age-based staleness flagged the normal case as
  a fault, and blocked the gate exactly when the step had been completed. What
  is checked is connection: a PV that is not connected has no value, the
  reading shows NOT CONNECTED and the gate will not pass. The UI does not
  flag old readings either.
- **Direction wording matches the physical adjuster, not the raw sign.**
  Vertical encoders read `-Y girder motion = +count`, so a target that needs
  the count to increase means the girder must go DOWN — the UI shows LOWER,
  not RAISE, for that case (and vice versa). Sway reads `+X = +count = 
  outboard`, so the UI shows "Move O/B" / "Move I/B" instead of a bare sign;
  surge is left as a plain +/- figure. Where a turn ratio is configured
  (`adjusters.mm_per_turn_vertical`, default 0.227 mm/turn; sway/surge are
  `adjusters.mm_per_turn_sway` / `mm_per_turn_surge`, unset by default since
  those ratios are not yet characterised) each active encoder also shows an
  estimated turn count alongside the mm figure.
- **Stage encoders are monitored during vertical moves.** They should barely
  move; if they do, the girder is riding a spherical bearing.
- **Expected values** are shown for monitored encoders, so "moved as expected
  because the girder tilted" is distinguishable from "moved unexpectedly".
- **A plan requires explicit confirmation.** A fit out of tolerance pauses in
  a "solved" phase — the pose error is shown, but no targets exist yet — until
  the operator presses *Accept survey — generate correction steps*. The survey
  and pose-error boxes then get out of the way for the move steps, and come
  back once every step is done (phase "verify") to prompt a confirmation
  survey; if that's still out of tolerance, it loops back to "solved" for
  another round rather than looping silently.
- **Step back is not undo.** The UI can rewind; the steel cannot. Re-opening a
  step recomputes its targets from the live pose — pushing live encoder
  deltas through the Jacobian, same mechanism as the model-validation check —
  rather than replaying the stale numbers from before any physical move. That
  estimate also accounts for whatever was already achieved in an *earlier,
  different* move group, whose own datum is superseded by the time you're
  back looking at it — but deliberately NOT for the group you're still
  *inside*: every group's own plan is a uniform, no-differential move (heave
  is a pure Y shift, never a shift-plus-tilt), so if only one of its two
  pair-moves is done the girder is genuinely asymmetric, and no single value
  fed into that uniform formula reproduces it correctly — the already-good
  pair's target would come out wrong. Left alone instead, it reproduces the
  same, still-correct plan for that group. If the pose has drifted more than
  a live reading can capture, *Abandon iteration* and re-survey — that is the
  honest recovery.
- **Overrides are recorded** with the operator's name and reason, and appear in
  the report.

### Geometry data

`_geom_data.py` is generated from the reference-point workbook and carries a
revision string recorded on every report — currently **2026-09-02-r2**.
Inboard/outboard and upstream/downstream are computed from each point's position
in the jack frame, never hand-labelled.

---

## Development

```bash
uv sync
uv run pytest                    # 155 tests
uv run ruff check src tests
uv run pyright src tests
uv run tox -p                    # everything CI runs
```

`cothread` (and `epicscorelibs`, which it uses for `libca`) ship no Windows or
macOS wheels, so `uv sync` skips both there and the suite runs against the
simulator only — `tests/test_threading.py`, which exercises the real
`CothreadBackend`, is skipped rather than failing. `serve` and `check` without
`--demo` still need Channel Access and so still need Linux; everything else,
including the full `--demo` walkthrough above, works as-is.

```
src/dls_deb_girder_alignment/
    server.py        Flask service and REST API
    geometry.py      AUTHORITATIVE maths: frames, Jacobian, pose fit, planner
    _geom_data.py    generated geometry for MS/LM/ML/SM
    _fea_data.py     FEA warp compliance and bare-girder heave pre-set
    epics_io.py      Channel Access via cothread, plus the simulator
    session.py       state machine, SQLite persistence, gating, plan datum
    report.py        PDF + JSON generation
    config.py        site config, environment overrides, PV resolution
    __main__.py      CLI
    static/          the UI
example/
    config/          deployment template: copy into a service's config/
    surveys/         example surveys for the demo, and a generator
scripts/
    mirror_reports.py   copy reports off a deployment to backed-up storage
    calibrate.py        suggest FEA/geometry corrections from session exports
```

The tests load `example/config/` and cover the geometry invariants the rest of
the tool depends on — Jacobian rank, the parasitic null space, the cross-shift
ratio, sway/surge decoupling, and that a survey generated from a known pose fits
back to that pose.
