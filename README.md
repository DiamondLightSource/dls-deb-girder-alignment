[![CI](https://github.com/DiamondLightSource/dls-deb-girder-alignment/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-deb-girder-alignment/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-deb-girder-alignment/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-deb-girder-alignment)
[![PyPI](https://img.shields.io/pypi/v/dls-deb-girder-alignment.svg)](https://pypi.org/project/dls-deb-girder-alignment)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_deb_girder_alignment

Build-bay alignment for Diamond-II accelerator girders. Takes a laser-tracker
survey, computes the correction, and walks a technician through the jack and
stage moves with live encoder readback, tolerance gating and a signed PDF record.

Supports all four girder fabrications: **MS, LM, ML, SM**.

Source          | <https://github.com/DiamondLightSource/dls-deb-girder-alignment>
:---:           | :---:
Docker          | `ghcr.io/diamondlightsource/dls-deb-girder-alignment:latest`
Releases        | <https://github.com/DiamondLightSource/dls-deb-girder-alignment/releases>

## Quick start

```bash
uv sync
uv run dls-deb-girder-alignment serve --demo    # simulator: training, no EPICS
uv run dls-deb-girder-alignment serve           # live Channel Access
```

Then open <http://localhost:8080/>.

`--demo` simulates a rigid girder: the encoders respond to a commanded pose the
way real ones would, including the cross-shift on the un-moved pair. That makes
it a **training mode**, not just a development convenience.

```bash
dls-deb-girder-alignment check            # prove CA reaches this bay's PVs
dls-deb-girder-alignment check --watch    # keep polling, to watch an encoder move
dls-deb-girder-alignment sessions list    # what is in the session store
```

## Why there is a backend

A browser cannot reach EPICS: Channel Access is raw UDP/TCP on ports 5064/5065
and JavaScript has no UDP socket. Once a service exists, session persistence,
PDF generation and a single authoritative maths implementation all follow.

**The alignment maths lives in `geometry.py` and is authoritative.** The browser
is a view. Do not re-implement the numerics in JavaScript — two copies will
drift, and a silent numerical discrepancy in this domain is expensive.

**EPICS access is read-only.** The tool never writes to an IOC; adjustment stays
manual. Do not add a `caput` path.

## One deployment per bay

Each build bay has its own encoder rig, so each bay gets its own deployment of
the same image. The only thing that changes is the EPICS domain:

| bay | `GIRDER_DOMAIN` | encoder PVs |
|---|---|---|
| 1 | `TS01C` | `TS01C-AL-GIRDR-01:US:Y_IB` … |
| 2 | `TS02C` | `TS02C-AL-GIRDR-01:US:Y_IB` … |

The **temperature sensors do not follow that convention**. They are one array
covering the whole hall, all under `TS01C`, and the bays map onto overlapping
subsets of it — so each bay is configured with an explicit list of full sensor PV
names rather than a domain and a set of suffixes.

Everything else is in `src/dls_deb_girder_alignment/data/config.yaml`, which
ships with the package as a default and can be replaced by a mounted file
(`--config`, or a ConfigMap). Environment overrides for deployment:

| variable | effect |
|---|---|
| `GIRDER_DOMAIN` | the bay's encoder domain, e.g. `TS02C` |
| `GIRDER_BAY` | bay label printed on the report |
| `GIRDER_SESSIONS_DB` | path to the SQLite session store |
| `GIRDER_REPORTS` | directory for generated reports |
| `GIRDER_MODELS` | directory holding the optional 3D girder models |

### 3D models are not in the image

`<type>_bare_girder.obj`/`.mtl`/`.stl` come to about 79 MB, so they are not
shipped in the package or the container. Point `GIRDER_MODELS` at a mounted
directory to enable the 3D view. Without it the plan and end views (pure SVG)
work as normal and the 3D view falls back to a plain envelope box.

## Workflow

1. **Start session** — girder serial (type resolves from `girder_serials.csv`),
   operator name, vertical mode, pairing, and the on-target gate.
2. **Load survey** — CSV of measured points, `X,Y,Z` per line; a name/index
   column is ignored. Points are matched to reference fiducials **by proximity**
   (10 mm gate), not by name, because names repeat between girder types.
3. **Move**, step by step. Each step shows the two active encoders large, with
   target, error and direction, plus monitors underneath.
4. **Confirm step** — blocked unless every active encoder is inside the gate, the
   parasitic monitors are in limit and no reading is stale. Override is possible
   and is recorded with name and reason.
5. **Re-survey** and iterate until every axis is in tolerance.
6. **Generate report** — PDF plus a JSON sibling.

### Adjustment order

Sequencing follows the **coupling**, not the tolerance priority: vertical first
(roll creeps X/Y through the geometric lever), horizontal last (nearly one-way
coupled, so it absorbs the creep without re-breaking the vertical).

**Sway and surge are separate steps and are fully independent.** They act along
perpendicular stage axes, and because a stage only translates, yaw — which comes
purely from differential sway — produces no surge-encoder motion.

**Why the two encoder families need different levers.** Vertical encoders contact
the girder underside, so they ride the rigid body and their own position is the
lever — that is what produces the ~0.049 mm/mm cross-shift between jack pairs.
Surge/sway encoders read *stage* translation, and a stage never rotates, so the
lever is the stage's location on the girder axis (the bearing-pair midpoint),
**not** the encoder position. Using the encoder position instead injects its
transverse offset and invents a surge/yaw coupling that does not exist.

### Sequential pairs and intermediate targets

Each vertical encoder sits ~325 mm along-axis from its own bearing, so moving one
pair re-tilts the girder about the other and shifts the un-moved pair's reading
by **≈0.049 mm per mm** of far-pair travel. **Only the last pair moved lands on
final targets**; the first pair stops at a compensated intermediate value.

## Two datums, and which is which

This is the thing to understand before changing anything in `session.py`.

**The hardware datum lives in the IOC.** Encoders are zeroed by setting
`…:X_SP` to zero and processing `…:X_ZCALC.PROC`; the offset is persisted by
autosave. That is a commissioning action performed when a girder goes on the
jacks, and this application never does it — the tool stays read-only.

**The plan datum lives in the session.** Step targets are encoder *deltas* from
the pose that was surveyed, so a reading only means something measured from the
value the encoder had when its move group opened. Move groups are `flatten`,
`heave` (or `combined`) and `horizontal`; heave targets assume flatten has
already been applied, so the reference has to be re-taken at each boundary or the
gate compares against the wrong thing.

The plan datum is captured **automatically** on entering a group, and is part of
the persisted session — so a pod restart mid-move resumes against the same
reference rather than silently changing it. There is no "zero display" button and
no operator step; getting this wrong was previously possible and invisible.

## Safety behaviours

- **Stale readings**: a frozen PV looks exactly like a stationary girder, so every
  reading carries its age and goes visibly stale rather than showing the last
  good value.
- **Stage encoders are monitored during vertical moves.** They should barely
  move; if they do, the girder is riding the spherical bearing.
- **Parasitic modes** WARP (vertical twist) and STRETCH (differential surge) are
  the two non-rigid encoder combinations — the left null space of the Jacobian.
  Warn 0.003 mm, stop 0.005 mm. The gate tests these too: on target with WARP out
  of limit is not a step to wave through.
- **Expected values** are shown for monitored encoders, so "moved as expected
  because the girder tilted" is distinguishable from "moved unexpectedly".
- **Step back is not undo.** The UI can rewind; the steel cannot. Re-opening a
  step never replays stale targets. If the pose has drifted, *Abandon iteration*
  and re-survey — that is the honest recovery.

## Geometry

`_geom_data.py` is generated from the reference-point workbook and carries a
revision string recorded on every report. Current revision: **2026-09-02-r2**.
Inboard/outboard and upstream/downstream are computed from each point's position
in the jack frame — never hand-labelled.

## Layout

```
src/dls_deb_girder_alignment/
    server.py        Flask service and REST API
    geometry.py      AUTHORITATIVE maths: frames, Jacobian, pose fit, planner
    _geom_data.py    generated geometry for MS/LM/ML/SM
    epics_io.py      Channel Access via cothread, plus the simulator; read-only
    session.py       state machine, SQLite persistence, gating, plan datum
    report.py        PDF + JSON generation
    config.py        site config, environment overrides, PV resolution
    __main__.py      CLI: serve / check / sessions
    static/          the UI
    data/            config.yaml and girder_serials.csv defaults
```
