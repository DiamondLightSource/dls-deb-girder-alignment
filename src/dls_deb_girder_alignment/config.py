"""Site configuration: loading, environment overrides, PV resolution.

NO SITE CONFIGURATION SHIPS WITH THIS PACKAGE. Real PV names, the bay's domain
and the girder serial table are deployment data, not code: they change without a
release and they differ per bay. The application is given a path and reads what
it finds there.

The path is resolved in this order:

1. ``--config`` on the command line;
2. ``$GIRDER_CONFIG``;
3. ``/epics/ioc/config/config.yaml`` - where a DLS ``*-services`` repo mounts a
   service's ``config/`` directory as a ConfigMap, so a deployed container needs
   no arguments at all.

``girder_serials.csv`` is looked for next to the config file, so both live in the
same ``config/`` directory and arrive in the same ConfigMap.

``example/config/`` in this repository is the template to copy into a service
directory. The test suite loads it, so it cannot rot.

One deployment serves one build bay. The bay is chosen by ``epics.domain``
(``TS01C`` for bay 1, ``TS02C`` for bay 2, ...) so that a single container image
serves every bay. Encoder PVs are built from that domain; temperature sensor PVs
are NOT, because the sensor array covers the whole hall under one domain, so
each bay is given an explicit list of full PV names.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import geometry as G

#: Where a DLS ``*-services`` repo mounts a service's ``config/`` directory.
DEFAULT_CONFIG_DIR = Path("/epics/ioc/config")
CONFIG_NAME = "config.yaml"
SERIALS_NAME = "girder_serials.csv"

#: Environment variables that override config file values at deploy time.
ENV_CONFIG = "GIRDER_CONFIG"
ENV_DOMAIN = "GIRDER_DOMAIN"
ENV_BAY = "GIRDER_BAY"
ENV_SESSIONS_DB = "GIRDER_SESSIONS_DB"
ENV_REPORTS = "GIRDER_REPORTS"
ENV_MODELS = "GIRDER_MODELS"


def find_config(explicit: Path | None = None) -> Path:
    """Locate the site config file. See the module docstring for the order."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"no config file at {explicit}")
        return explicit

    from_env = os.environ.get(ENV_CONFIG)
    if from_env:
        path = Path(from_env).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"${ENV_CONFIG} points at {path}, which is not a file"
            )
        return path

    mounted = DEFAULT_CONFIG_DIR / CONFIG_NAME
    if mounted.is_file():
        return mounted

    raise FileNotFoundError(
        "no site configuration found. This package ships none: pass --config "
        f"<path>, set ${ENV_CONFIG}, or mount one at {mounted}. "
        "Copy example/config/ from the source repository to start."
    )


@dataclass
class Config:
    """Resolved site configuration for one bay."""

    domain: str
    encoder_device: str
    pv_map: dict[str, str]
    """Encoder id -> fully resolved PV name."""
    scale: dict[str, float]
    zero_setpoint_suffix: str
    """Suffix of the zero setpoint PV, written to zero when zeroing."""
    zero_process_suffix: str
    """Suffix of the record processed to compute and apply the zero offset."""
    sensor_pvs: list[str]
    """Full temperature sensor PV names for this bay."""
    max_spread_c: float
    gate_default_mm: float
    encoder_resolution_mm: float
    sessions_db: Path
    reports: Path
    models: Path | None
    bay: str = ""
    """Human-readable bay label, recorded on the report. Cosmetic."""
    config_path: Path | None = None
    """Where the configuration was read from, for the startup banner."""
    serials: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def device_prefix(self) -> str:
        return f"{self.domain}-{self.encoder_device}"


def load(path: Path | None = None, serials_path: Path | None = None) -> Config:
    """Load configuration, applying environment overrides."""
    config_path = find_config(path)
    raw = yaml.safe_load(config_path.read_text()) or {}
    ep = raw.get("epics") or {}
    temp = raw.get("temperature") or {}
    gate = raw.get("gate") or {}
    paths = raw.get("paths") or {}

    domain = os.environ.get(ENV_DOMAIN) or ep.get("domain") or ""
    if not domain:
        raise ValueError(
            f"no EPICS domain configured - set epics.domain or ${ENV_DOMAIN}"
        )
    device = ep.get("encoder_device") or ""
    if not device:
        raise ValueError("no epics.encoder_device configured")

    suffixes: dict[str, Any] = ep.get("pv_map") or {}
    missing = [e for e in G.ENCODER_IDS if e not in suffixes]
    if missing:
        raise ValueError(f"epics.pv_map is missing encoder(s): {', '.join(missing)}")
    prefix = f"{domain}-{device}"
    pv_map = {eid: f"{prefix}:{suffixes[eid]}" for eid in G.ENCODER_IDS}

    sensors = temp.get("sensor_pvs") or []
    if isinstance(sensors, dict):
        # Tolerate the older mapping form; the values are the full PV names.
        sensors = list(sensors.values())

    models_raw = os.environ.get(ENV_MODELS) or paths.get("models") or ""

    return Config(
        domain=domain,
        encoder_device=device,
        pv_map=pv_map,
        scale={k: float(v) for k, v in (ep.get("scale") or {}).items()},
        zero_setpoint_suffix=ep.get("zero_setpoint_suffix", "_SP"),
        zero_process_suffix=ep.get("zero_process_suffix", "_ZCALC.PROC"),
        sensor_pvs=[str(s) for s in sensors],
        max_spread_c=float(temp.get("max_spread_c", 0.5)),
        gate_default_mm=float(gate.get("default_mm", 0.010)),
        encoder_resolution_mm=float(gate.get("encoder_resolution_mm", 0.001)),
        sessions_db=Path(
            os.environ.get(ENV_SESSIONS_DB) or paths.get("sessions_db") or "sessions.db"
        ).expanduser(),
        reports=Path(
            os.environ.get(ENV_REPORTS) or paths.get("reports") or "reports"
        ).expanduser(),
        models=Path(models_raw).expanduser() if models_raw else None,
        bay=os.environ.get(ENV_BAY, ""),
        config_path=config_path,
        serials=load_serials(_serials_path(config_path, paths, serials_path)),
    )


def _serials_path(
    config_path: Path, paths: dict[str, Any], explicit: Path | None
) -> Path:
    """Locate the serial table, by default beside the config file.

    Keeping it next to the config means both files sit in one ``config/``
    directory and reach the pod in one ConfigMap.
    """
    if explicit is not None:
        return explicit
    configured = paths.get("serials")
    if configured:
        # Relative to the config file, not the working directory: the pair
        # travels together.
        return (config_path.parent / Path(configured)).resolve()
    return config_path.parent / SERIALS_NAME


def load_serials(path: Path) -> dict[str, dict[str, str]]:
    """Serial -> girder type lookup.

    Serials do not encode the girder type, so this table is the authority. An
    unknown serial is not an error: the operator picks the type by hand.
    """
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    with path.open() as f:
        rows = csv.DictReader(r for r in f if not r.lstrip().startswith("#"))
        for row in rows:
            serial = (row.get("serial") or "").strip()
            gtype = (row.get("type") or "").strip().upper()
            if serial and gtype in G.GEOM:
                out[serial.upper()] = {
                    "type": gtype,
                    "notes": (row.get("notes") or "").strip(),
                }
    return out
