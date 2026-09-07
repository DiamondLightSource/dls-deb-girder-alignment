"""Shared fixtures.

The key helper is :func:`survey_for`, which manufactures a laser-tracker survey
for a girder placed at a known pose error. Every test that needs a survey builds
it by applying a pose to the reference fiducials, so the expected answer is known
exactly rather than being read off a recorded file.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from dls_deb_girder_alignment import config
from dls_deb_girder_alignment import geometry as G

GIRDER_TYPES = sorted(G.GEOM)

#: No site configuration ships with the package, so the tests load the template
#: from example/config/. That keeps the template honest: if it stops being a
#: valid configuration, the suite fails.
EXAMPLE_CONFIG = pathlib.Path(__file__).parent.parent / "example" / "config"
EXAMPLE_CONFIG_FILE = EXAMPLE_CONFIG / "config.yaml"


#: Every EPICS_CA_* variable the config can set.
CA_VARS = tuple(config.CA_KEYS.values())


@pytest.fixture(autouse=True)
def _clean_ca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep EPICS_CA_* out of the tests.

    ``config.load`` writes these into ``os.environ`` - it has to, because libca
    reads them once when it loads and never again - so without this the first
    test to load a config leaks its settings into every test after it and the
    ordering decides the result.
    """
    for name in CA_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(params=GIRDER_TYPES)
def machine(request: pytest.FixtureRequest) -> G.Machine:
    """Each girder type in turn."""
    return G.machine(request.param)


@pytest.fixture
def ms() -> G.Machine:
    return G.machine("MS")


@pytest.fixture
def survey_for():
    """Callable building a survey for a girder at a known pose error."""
    return _survey_for


def _survey_for(m: G.Machine, err: dict[str, float]) -> list[list[float]]:
    """Fiducial positions for a girder sitting at pose error ``err``.

    ``err`` is in the machine frame (mm for sway/heave/surge, mrad for
    roll/pitch/yaw), the same convention :func:`geometry.fit_pose` returns.
    """
    v = m.to_master(err)
    t, th = v[:3], v[3:]
    pts = []
    for nom in m.fids.values():
        r = nom - m.p0
        pts.append(list(nom + t + np.cross(th, r)))
    return pts


@pytest.fixture
def example_config() -> pathlib.Path:
    """The deployment template in example/config/."""
    return EXAMPLE_CONFIG_FILE


@pytest.fixture
def cfg(tmp_path):
    """Config pointed at a scratch directory, so tests never touch real state."""
    c = config.load(EXAMPLE_CONFIG_FILE)
    c.sessions_db = tmp_path / "sessions.sqlite"
    c.reports = tmp_path / "reports"
    return c
