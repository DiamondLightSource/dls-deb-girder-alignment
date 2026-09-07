"""Configuration loading and per-bay PV resolution."""

from __future__ import annotations

import pytest
import yaml

from dls_deb_girder_alignment import config
from dls_deb_girder_alignment import geometry as G


def test_packaged_defaults_load():
    cfg = config.load()
    assert cfg.domain == "TS01C"
    assert set(cfg.pv_map) == set(G.ENCODER_IDS)
    assert cfg.pv_map["V_US_IN"] == "TS01C-AL-GIRDR-01:US:Y_IB"
    assert cfg.serials["DLS0011116"]["type"] == "MS"


def test_domain_is_the_only_per_bay_knob(tmp_path, monkeypatch):
    """Bay 2 is the same image with a different domain."""
    monkeypatch.setenv(config.ENV_DOMAIN, "TS02C")
    cfg = config.load()
    assert cfg.device_prefix == "TS02C-AL-GIRDR-01"
    assert all(pv.startswith("TS02C-AL-GIRDR-01:") for pv in cfg.pv_map.values())


def test_temperature_sensors_do_not_follow_the_encoder_domain(monkeypatch):
    """One sensor array covers the hall, so the sensors stay on TS01C."""
    monkeypatch.setenv(config.ENV_DOMAIN, "TS02C")
    cfg = config.load()
    assert cfg.sensor_pvs
    assert all(pv.startswith("TS01C-EA-GIRDR-01:") for pv in cfg.sensor_pvs)


def test_paths_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_SESSIONS_DB, str(tmp_path / "s.sqlite"))
    monkeypatch.setenv(config.ENV_REPORTS, str(tmp_path / "reports"))
    cfg = config.load()
    assert cfg.sessions_db == tmp_path / "s.sqlite"
    assert cfg.reports == tmp_path / "reports"


def test_missing_encoder_is_rejected(tmp_path):
    """A partial pv_map must fail loudly, not silently drop an encoder."""
    raw = yaml.safe_load(config.DEFAULT_CONFIG.read_text())
    del raw["epics"]["pv_map"]["SWAY_DS"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="SWAY_DS"):
        config.load(path)


def test_missing_domain_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_DOMAIN, raising=False)
    raw = yaml.safe_load(config.DEFAULT_CONFIG.read_text())
    raw["epics"]["domain"] = ""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="domain"):
        config.load(path)


def test_unknown_serials_are_simply_absent(tmp_path):
    path = tmp_path / "serials.csv"
    path.write_text("serial,type,notes\nDLS9999999,MS,\nBAD,NOTATYPE,\n")
    serials = config.load_serials(path)
    assert serials == {"DLS9999999": {"type": "MS", "notes": ""}}
