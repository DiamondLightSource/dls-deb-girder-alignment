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


# ---------------------------------------------------------------------------
# Channel Access settings
#
# These are the settings whose failure mode is indistinguishable from a dead
# IOC: the wrong port, or no route to the IOCs, reads as a plain timeout.
# ---------------------------------------------------------------------------


def test_the_example_config_selects_the_deb_ca_port(example_config):
    """The build area serves CA on 6064, not the 5064 default."""
    cfg = config.load(example_config)
    assert cfg.ca["EPICS_CA_SERVER_PORT"] == "6064"


def test_the_repeater_port_follows_the_server_port():
    """What DLS `. changeports 6064` does. Base does not do it for us.

    Base's EPICS_CA_REPEATER_PORT default is a flat 5065 whatever the server
    port is, so a config that moves the server port and says nothing else would
    otherwise leave the client on another port zone's repeater.
    """
    assert config.resolve_ca({"server_port": 6064}) == {
        "EPICS_CA_SERVER_PORT": "6064",
        "EPICS_CA_REPEATER_PORT": "6065",
    }


def test_an_explicit_repeater_port_is_not_overridden():
    ca = config.resolve_ca({"server_port": 6064, "repeater_port": 5065})
    assert ca["EPICS_CA_REPEATER_PORT"] == "5065"


def test_ca_keys_map_to_epics_variables():
    ca = config.resolve_ca(
        {
            "auto_address_list": "NO",
            "name_servers": "deb-epics-gateways:5064",
            "address_list": "172.23.0.1",
        }
    )
    assert ca == {
        "EPICS_CA_AUTO_ADDR_LIST": "NO",
        "EPICS_CA_NAME_SERVERS": "deb-epics-gateways:5064",
        "EPICS_CA_ADDR_LIST": "172.23.0.1",
    }


def test_a_full_epics_variable_name_passes_through():
    """Nothing libca understands is unreachable from the config file."""
    assert config.resolve_ca({"EPICS_CA_MCAST_TTL": 4}) == {"EPICS_CA_MCAST_TTL": "4"}


def test_a_misspelt_ca_key_is_an_error():
    with pytest.raises(ValueError, match="unknown epics.ca key 'severport'"):
        config.resolve_ca({"severport": 6064})


def test_empty_ca_values_are_skipped():
    """So a deployment can comment a setting out by blanking it."""
    assert config.resolve_ca({"name_servers": "", "address_list": None}) == {}


def test_loading_puts_the_ca_settings_into_the_environment(example_config):
    """libca reads EPICS_CA_* when it loads, so the config must reach it there."""
    import os

    config.load(example_config)
    assert os.environ["EPICS_CA_SERVER_PORT"] == "6064"
    assert os.environ["EPICS_CA_REPEATER_PORT"] == "6065"


def test_the_environment_beats_the_config_file(monkeypatch, example_config):
    """A pod knows more about the network it landed in than the ConfigMap does."""
    monkeypatch.setenv("EPICS_CA_SERVER_PORT", "5064")
    cfg = config.load(example_config)
    assert cfg.ca["EPICS_CA_SERVER_PORT"] == "5064"


def test_a_config_with_no_ca_block_changes_nothing(
    monkeypatch, tmp_path, example_config
):
    """Defaults stay the CA defaults: no surprise port for a plain config."""
    import os

    raw = yaml.safe_load(example_config.read_text())
    raw["epics"].pop("ca")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert config.load(path).ca == {}
    assert "EPICS_CA_SERVER_PORT" not in os.environ
