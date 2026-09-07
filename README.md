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

1. Serial `DLS0011116`, any operator name, turn **Combined vertical** on, **Start session**.
2. Load `example/surveys/ms_DLS0011116_initial.csv`. The fit comes back at about
   0.02 mm RMS with every axis out of tolerance, and a four-step plan appears.
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

**The tool never commands motion.** Adjustment stays manual. There is exactly one
write path — *Zero encoders* — and it is confirmed by the operator first.

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

**The hardware datum lives in the IOC.** *Zero encoders* in the move card sets
each `…:X_SP` to zero and processes `…:X_ZCALC.PROC`; the IOC computes the offset
and autosave persists it. The suffixes are configurable. This is not undoable
from the tool and everything else reading those PVs sees the change, so the
button confirms first, the action goes into the audit trail with the operator's
name, and a partial failure is reported per encoder rather than swallowed.

**The plan datum lives in the session.** Step targets are encoder *deltas* from
the pose that was surveyed, so a reading only means something measured from the
value the encoder had when its move group opened. Move groups are `flatten`,
`heave` (or `combined`) and `horizontal`; heave targets assume flatten has
already been applied, so the reference has to be re-taken at each boundary or the
gate compares against the wrong thing.

The plan datum is captured **automatically** on entering a group and is part of
the persisted session, so a restart mid-move resumes against the same reference
rather than silently changing it. Zeroing the hardware drops it, since the
absolute readings have just moved.

### Safety behaviours

- **Parasitic modes.** Eight encoders, six degrees of freedom — so two encoder
  combinations exist that a *rigid* girder cannot produce. They are named WARP
  (vertical twist) and STRETCH (differential surge), and they are the left null
  space of the Jacobian. Warn at 0.003 mm, stop at 0.005 mm. The gate tests them
  too: on target with WARP out of limit is not a step to wave through.
- **Stale readings.** A frozen PV looks exactly like a stationary girder, so
  every reading carries its age and goes visibly stale rather than showing the
  last good value.
- **Stage encoders are monitored during vertical moves.** They should barely
  move; if they do, the girder is riding a spherical bearing.
- **Expected values** are shown for monitored encoders, so "moved as expected
  because the girder tilted" is distinguishable from "moved unexpectedly".
- **Step back is not undo.** The UI can rewind; the steel cannot. Re-opening a
  step never replays stale targets. If the pose has drifted, *Abandon iteration*
  and re-survey — that is the honest recovery.
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
uv run pytest                    # 94 tests
uv run ruff check src tests
uv run pyright src tests
uv run tox -p                    # everything CI runs
```

```
src/dls_deb_girder_alignment/
    server.py        Flask service and REST API
    geometry.py      AUTHORITATIVE maths: frames, Jacobian, pose fit, planner
    _geom_data.py    generated geometry for MS/LM/ML/SM
    epics_io.py      Channel Access via cothread, plus the simulator
    session.py       state machine, SQLite persistence, gating, plan datum
    report.py        PDF + JSON generation
    config.py        site config, environment overrides, PV resolution
    __main__.py      CLI
    static/          the UI
example/
    config/          deployment template: copy into a service's config/
    surveys/         example surveys for the demo, and a generator
```

The tests load `example/config/` and cover the geometry invariants the rest of
the tool depends on — Jacobian rank, the parasitic null space, the cross-shift
ratio, sway/surge decoupling, and that a survey generated from a known pose fits
back to that pose.
