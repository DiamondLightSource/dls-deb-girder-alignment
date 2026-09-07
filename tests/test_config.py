"""Configuration loading, discovery order and per-bay PV resolution."""

from __future__ import annotations

import pytest
import yaml

from dls_deb_girder_alignment import config
from dls_deb_girder_alignment import geometry as G


def test_example_config_is_valid(example_config):
    """The template in example/config/ must stay a working configuration."""
    cfg = config.load(example_config)
    assert cfg.domain == "TS01C"
    assert set(cfg.pv_map) == set(G.ENCODER_IDS)
    assert cfg.pv_map["V_US_IN"] == "TS01C-AL-GIRDR-01:US:Y_IB"
    assert cfg.serials["DLS0011116"]["type"] == "MS"
    assert cfg.config_path == example_config


def test_no_configuration_ships_with_the_package(monkeypatch, tmp_path):
    """Site data is deployment data. Finding none must fail with instructions."""
    monkeypatch.delenv(config.ENV_CONFIG, raising=False)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", tmp_path / "nothing-here")
    with pytest.raises(FileNotFoundError, match="no site configuration found"):
        config.load()


def test_config_is_found_in_the_mounted_config_dir(monkeypatch, example_config):
    """A deployed container needs no arguments: the ConfigMap mount is default."""
    monkeypatch.delenv(config.ENV_CONFIG, raising=False)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", example_config.parent)
    assert config.load().domain == "TS01C"


def test_environment_beats_the_mounted_default(monkeypatch, tmp_path, example_config):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", tmp_path / "nothing-here")
    monkeypatch.setenv(config.ENV_CONFIG, str(example_config))
    assert config.load().config_path == example_config


def test_explicit_path_beats_the_environment(monkeypatch, tmp_path, example_config):
    monkeypatch.setenv(config.ENV_CONFIG, str(tmp_path / "wrong.yaml"))
    assert config.load(example_config).config_path == example_config


def test_a_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no config file at"):
        config.load(tmp_path / "absent.yaml")


def test_serials_are_found_beside_the_config(tmp_path, example_config):
    """Both files live in one config/ directory, so one ConfigMap carries them."""
    (tmp_path / "config.yaml").write_text(example_config.read_text())
    (tmp_path / "girder_serials.csv").write_text("serial,type,notes\nDLS1,LM,\n")
    cfg = config.load(tmp_path / "config.yaml")
    assert cfg.serials == {"DLS1": {"type": "LM", "notes": ""}}


def test_domain_is_the_only_per_bay_knob(example_config, monkeypatch):
    """Bay 2 is the same image with a different domain."""
    monkeypatch.setenv(config.ENV_DOMAIN, "TS02C")
    cfg = config.load(example_config)
    assert cfg.device_prefix == "TS02C-AL-GIRDR-01"
    assert all(pv.startswith("TS02C-AL-GIRDR-01:") for pv in cfg.pv_map.values())


def test_temperature_sensors_do_not_follow_the_domain(example_config, monkeypatch):
    """One sensor array covers the hall, so the sensors stay on TS01C."""
    monkeypatch.setenv(config.ENV_DOMAIN, "TS02C")
    cfg = config.load(example_config)
    assert cfg.sensor_pvs
    assert all(pv.startswith("TS01C-EA-GIRDR-01:") for pv in cfg.sensor_pvs)


def test_paths_come_from_the_environment(tmp_path, monkeypatch, example_config):
    monkeypatch.setenv(config.ENV_SESSIONS_DB, str(tmp_path / "s.sqlite"))
    monkeypatch.setenv(config.ENV_REPORTS, str(tmp_path / "reports"))
    cfg = config.load(example_config)
    assert cfg.sessions_db == tmp_path / "s.sqlite"
    assert cfg.reports == tmp_path / "reports"


def test_missing_encoder_is_rejected(tmp_path, example_config):
    """A partial pv_map must fail loudly, not silently drop an encoder."""
    raw = yaml.safe_load(example_config.read_text())
    del raw["epics"]["pv_map"]["SWAY_DS"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="SWAY_DS"):
        config.load(path)


def test_missing_domain_is_rejected(tmp_path, monkeypatch, example_config):
    monkeypatch.delenv(config.ENV_DOMAIN, raising=False)
    raw = yaml.safe_load(example_config.read_text())
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


def test_zero_suffixes_are_configurable_site_data(example_config):
    cfg = config.load(example_config)
    assert cfg.zero_setpoint_suffix == "_SP"
    assert cfg.zero_process_suffix == "_ZCALC.PROC"
