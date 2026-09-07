"""Site configuration: loading, environment overrides, PV resolution.

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
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from . import geometry as G

PACKAGE = "dls_deb_girder_alignment"

#: Environment variables that override config file values at deploy time.
ENV_DOMAIN = "GIRDER_DOMAIN"
ENV_BAY = "GIRDER_BAY"
ENV_SESSIONS_DB = "GIRDER_SESSIONS_DB"
ENV_REPORTS = "GIRDER_REPORTS"
ENV_MODELS = "GIRDER_MODELS"


def _packaged(name: str) -> Path:
    """Path to a file shipped in the package's ``data`` directory."""
    return Path(str(resources.files(PACKAGE) / "data" / name))


DEFAULT_CONFIG = _packaged("config.yaml")
DEFAULT_SERIALS = _packaged("girder_serials.csv")


@dataclass
class Config:
    """Resolved site configuration for one bay."""

    domain: str
    encoder_device: str
    pv_map: dict[str, str]
    """Encoder id -> fully resolved PV name."""
    scale: dict[str, float]
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
    serials: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def device_prefix(self) -> str:
        return f"{self.domain}-{self.encoder_device}"


def load(path: Path | None = None, serials_path: Path | None = None) -> Config:
    """Load configuration, applying environment overrides.

    ``path`` defaults to the copy shipped with the package, which is what makes
    ``dls-deb-girder-alignment serve --demo`` work with no arguments.
    """
    raw = yaml.safe_load((path or DEFAULT_CONFIG).read_text()) or {}
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
        serials=load_serials(serials_path or DEFAULT_SERIALS),
    )


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
