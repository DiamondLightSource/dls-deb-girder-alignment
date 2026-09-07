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

Channel Access settings live under ``epics.ca`` and are pushed into the process
environment by :func:`apply_ca_env`, because ``libca`` reads ``EPICS_CA_*`` once,
when it is first loaded, and never again.
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

#: ``epics.ca`` keys -> the Channel Access environment variable each one sets.
#: A key already spelled ``EPICS_...`` is passed through unchanged, so a setting
#: with no friendly name here is still reachable from the config file.
CA_KEYS = {
    "server_port": "EPICS_CA_SERVER_PORT",
    "repeater_port": "EPICS_CA_REPEATER_PORT",
    "auto_address_list": "EPICS_CA_AUTO_ADDR_LIST",
    "address_list": "EPICS_CA_ADDR_LIST",
    "name_servers": "EPICS_CA_NAME_SERVERS",
    "connection_timeout": "EPICS_CA_CONN_TMO",
    "max_array_bytes": "EPICS_CA_MAX_ARRAY_BYTES",
}


def resolve_ca(ca: dict[str, Any]) -> dict[str, str]:
    """Turn an ``epics.ca`` block into ``EPICS_CA_*`` name/value pairs.

    ``EPICS_CA_REPEATER_PORT`` is derived as ``server_port + 1`` when it is not
    given. Base does not derive it - its default is a flat 5065 whatever the
    server port is - so moving the server port and forgetting the repeater
    leaves the client on the repeater of a different port zone. This is what
    DLS ``. changeports <port>`` does by hand, and doing it here means a config
    that says 6064 needs to say nothing else.
    """
    out: dict[str, str] = {}
    for key, value in ca.items():
        if value is None or value == "":
            continue
        name = CA_KEYS.get(key, key if str(key).startswith("EPICS_") else "")
        if not name:
            raise ValueError(
                f"unknown epics.ca key {key!r} - expected one of "
                f"{', '.join(sorted(CA_KEYS))}, or a full EPICS_* variable name"
            )
        out[name] = str(value)
    server = out.get("EPICS_CA_SERVER_PORT")
    if server and "EPICS_CA_REPEATER_PORT" not in out:
        out["EPICS_CA_REPEATER_PORT"] = str(int(server) + 1)
    return out


def apply_ca_env(ca: dict[str, str]) -> dict[str, str]:
    """Put Channel Access settings into the environment, and report the result.

    This has to run before anything imports ``cothread``: ``libca`` reads these
    when it loads and caches them for the life of the process.

    A variable already set in the environment wins. The deployment knows more
    about the network it landed in than the ConfigMap does, so a pod can be
    pointed at a different gateway without editing the config.
    """
    for name, value in ca.items():
        os.environ.setdefault(name, value)
    return {name: os.environ[name] for name in ca}


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
    ca: dict[str, str]
    """``EPICS_CA_*`` settings in force, after the environment has had its say."""
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

    # Applied before the Config exists, and so before any backend imports libca.
    ca = apply_ca_env(resolve_ca(ep.get("ca") or {}))

    return Config(
        domain=domain,
        encoder_device=device,
        pv_map=pv_map,
        scale={k: float(v) for k, v in (ep.get("scale") or {}).items()},
        zero_setpoint_suffix=ep.get("zero_setpoint_suffix", "_SP"),
        zero_process_suffix=ep.get("zero_process_suffix", "_ZCALC.PROC"),
        sensor_pvs=[str(s) for s in sensors],
        ca=ca,
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
