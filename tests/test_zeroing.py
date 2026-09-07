"""Encoder zeroing - the only write this tool performs.

Zeroing sets a hardware datum in the IOC, persisted by autosave. It is visible to
everything else on the machine and cannot be undone from here, so it must be
explicitly confirmed and must invalidate the session's plan datum.
"""

from __future__ import annotations

import pytest

from dls_deb_girder_alignment import epics_io
from dls_deb_girder_alignment import geometry as G
from dls_deb_girder_alignment.server import Service, create_app


class RecordingBackend(epics_io.LiveBackend):
    """A live-like backend that records writes instead of performing them."""

    name = "recording"

    def __init__(self, fail: set[str] | None = None):
        self.writes: list[tuple[str, float]] = []
        self.fail = fail or set()

    def read(self, pvs):
        return {p: epics_io.Reading(p, 0.0, 0.0, True) for p in pvs}

    def write(self, pv: str, value: float) -> None:
        if any(pv.startswith(f) for f in self.fail):
            raise RuntimeError("write failed")
        self.writes.append((pv, value))


@pytest.fixture
def enc(cfg):
    return epics_io.EncoderService(
        cfg.pv_map,
        RecordingBackend(),
        zero_setpoint_suffix=cfg.zero_setpoint_suffix,
        zero_process_suffix=cfg.zero_process_suffix,
    )


def test_zero_writes_setpoint_then_process(enc, cfg):
    """Set the setpoint to zero, then process the calc - in that order."""
    res = enc.zero_encoders()
    assert not res["errors"]
    assert res["encoders"] == list(cfg.pv_map)

    writes = enc.backend.writes
    assert len(writes) == 2 * len(G.ENCODER_IDS)
    pv = cfg.pv_map["V_US_IN"]
    assert writes[0] == (f"{pv}_SP", 0.0)
    assert writes[1] == (f"{pv}_ZCALC.PROC", 1)


def test_zero_suffixes_are_configurable(cfg):
    backend = RecordingBackend()
    enc = epics_io.EncoderService(
        cfg.pv_map,
        backend,
        zero_setpoint_suffix=":SETP",
        zero_process_suffix=":GO.PROC",
    )
    enc.zero_encoders(["SWAY_US"])
    pv = cfg.pv_map["SWAY_US"]
    assert backend.writes == [(f"{pv}:SETP", 0.0), (f"{pv}:GO.PROC", 1)]


def test_zero_can_target_named_encoders(enc, cfg):
    res = enc.zero_encoders(["V_US_IN", "V_DS_OUT"])
    assert res["encoders"] == ["V_US_IN", "V_DS_OUT"]
    assert len(enc.backend.writes) == 4


def test_zero_rejects_an_unknown_encoder(enc):
    with pytest.raises(ValueError, match="NOT_AN_ENCODER"):
        enc.zero_encoders(["NOT_AN_ENCODER"])


def test_partial_failure_is_reported_per_encoder(cfg):
    """A half-zeroed rig is a mixed state; the operator must be told which."""
    pv = cfg.pv_map["SURGE_DS"]
    enc = epics_io.EncoderService(cfg.pv_map, RecordingBackend(fail={pv}))
    res = enc.zero_encoders()
    assert set(res["errors"]) == {"SURGE_DS"}
    assert "SURGE_DS" not in res["encoders"]
    assert len(res["encoders"]) == len(G.ENCODER_IDS) - 1


def test_demo_zero_makes_the_encoders_read_zero(cfg):
    backend = epics_io.DemoBackend()
    enc = epics_io.EncoderService(cfg.pv_map, backend)
    backend.set_targets(dict.fromkeys(cfg.pv_map.values(), 1.5))
    for _ in range(200):
        enc.read_all()

    before = enc.read_all()
    assert abs(before["V_US_IN"]["absolute"]) > 1.0

    enc.zero_encoders()
    after = enc.read_all()
    for eid in G.ENCODER_IDS:
        assert abs(after[eid]["absolute"]) < 0.01


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------
@pytest.fixture
def client(cfg):
    service = Service(cfg, demo=True)
    with create_app(service).test_client() as c:
        yield c, service


def test_zero_requires_confirmation(client):
    c, _ = client
    r = c.post("/api/encoders/zero", json={})
    assert r.status_code == 400
    assert "confirm" in r.get_json()["error"]


def test_zero_through_the_api(client):
    c, _ = client
    r = c.post("/api/encoders/zero", json={"confirm": True})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_zero_invalidates_the_plan_datum(client, survey_for):
    """Absolutes have moved, so a datum taken before the zero is meaningless."""
    c, service = client
    c.post("/api/session", json={"serial": "DLS0011116", "operator": "tester"})
    c.post("/api/survey", json={"points": survey_for(G.machine("MS"), {"roll": 0.4})})
    c.get("/api/encoders")
    assert service.session is not None
    assert service.session.datum_group == "flatten"

    c.post("/api/encoders/zero", json={"confirm": True, "operator": "Sam"})
    assert service.session.datum == {}
    assert service.session.datum_group == ""

    # The next poll re-takes it against the new frame.
    c.get("/api/encoders")
    assert service.session.datum_group == "flatten"
    assert all(abs(v) < 0.01 for v in service.session.datum.values())


def test_zero_is_recorded_in_the_audit_trail(client, survey_for):
    c, service = client
    c.post("/api/session", json={"serial": "DLS0011116", "operator": "tester"})
    c.post("/api/encoders/zero", json={"confirm": True, "operator": "Sam"})
    assert service.session is not None
    events = [e for e in service.session.events if e["kind"] == "encoder_zero"]
    assert len(events) == 1
    assert events[0]["by"] == "Sam"
    assert set(events[0]["encoders"]) == set(G.ENCODER_IDS)


def test_zero_failure_reports_which_encoders(cfg, survey_for):
    """A failing IOC write must surface as an error, not a silent success."""
    service = Service(cfg, demo=True)
    service.enc.backend = RecordingBackend(fail={cfg.pv_map["SWAY_US"]})
    with create_app(service).test_client() as c:
        r = c.post("/api/encoders/zero", json={"confirm": True})
    assert r.status_code == 502
    body = r.get_json()
    assert body["ok"] is False
    assert "SWAY_US" in body["error"]
