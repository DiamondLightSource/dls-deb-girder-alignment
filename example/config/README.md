# Site configuration for one build bay

Copy this directory into a service in a `*-services` repository, as
`services/<service-name>/config/`. Every file in a service's `config/` becomes a
key in a ConfigMap and is mounted at `/epics/ioc/config`, which is where
`dls-deb-girder-alignment` looks for `config.yaml` by default — so a deployed
container needs no arguments.

```
services/ts01c-girder-align/
├── Chart.yaml
├── config/
│   ├── config.yaml           <- this directory
│   └── girder_serials.csv
└── values.yaml
```

Nothing here ships with the Python package. Real PV names, the bay's domain and
the girder serial table are deployment data: they change without a release and
they differ per bay.

## What to change per bay

**`epics.domain`** — the only per-bay knob for the encoders. `TS01C` for bay 1,
`TS02C` for bay 2. Every encoder PV is built from it, so the container image is
identical for every bay. It can also be set with `$GIRDER_DOMAIN`, which wins
over the file.

**`temperature.sensor_pvs`** — full PV names, and they do *not* follow
`epics.domain`. The sensors are one array covering the whole hall, all under
`TS01C`, and the bays map onto overlapping subsets of it:

| bay | sensors |
|---|---|
| 1 | `TMON01`, `TMON02`, `TMON03` |
| 2 | `TMON02`, `TMON03`, `TMON04`, `TMON05` |
| 3 | `TMON04`, `TMON05`, `TMON06` |

**`girder_serials.csv`** — serials do not encode the girder type, so this table
is the authority. It is the same for every bay, so it is duplicated across the
services; an unknown serial is not an error, the operator picks the type by hand
and is prompted to add the row.

## Running against it locally

```bash
dls-deb-girder-alignment serve --demo --config example/config/config.yaml
```

`--config` beats `$GIRDER_CONFIG`, which beats `/epics/ioc/config/config.yaml`.
