"""End-to-end: a whole alignment driven through the HTTP API in demo mode."""

from __future__ import annotations

from typing import NamedTuple

import pytest
from flask.testing import FlaskClient

from dls_deb_girder_alignment import geometry as G
from dls_deb_girder_alignment.server import Service, create_app, parse_points


class Harness(NamedTuple):
    """A test client plus the Service behind it, for asserting on server state."""

    client: FlaskClient
    service: Service


@pytest.fixture
def client(cfg):
    with Service(cfg, demo=True) as service:
        app = create_app(service)
        app.config.update(TESTING=True)
        with app.test_client() as c:
            yield Harness(c, service)


def _json(resp):
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def _error(resp) -> str:
    """Validation failures come back as HTTP 400 with an explanation."""
    assert resp.status_code == 400, resp.data
    body = resp.get_json()
    assert body["ok"] is False
    return body["error"]


def _settle(client, limit: int = 200) -> dict:
    """Poll until the simulated encoders reach the step's targets."""
    for _ in range(limit):
        enc = _json(client.client.get("/api/encoders"))
        if enc.get("check", {}).get("ok"):
            return enc
    return enc


def test_info_reports_this_bay(client):
    info = _json(client.client.get("/api/info"))
    assert info["demo"] is True
    assert info["domain"] == "TS01C"
    assert info["live_backend"] == "demo"
    assert set(info["encoders"]) == set(G.ENCODER_IDS)
    assert "MS" in info["types"]


def test_serial_lookup(client):
    assert _json(client.client.get("/api/serial/DLS0011116"))["type"] == "MS"
    unknown = _json(client.client.get("/api/serial/NOPE"))
    assert unknown["known"] is False


def test_ui_and_missing_model_are_served_sanely(client):
    resp = client.client.get("/")
    assert b"Girder Align" in resp.data
    resp.close()  # send_from_directory holds the file open until it is
    # 3D models are not shipped; the viewer falls back on its own.
    assert client.client.get("/ms_bare_girder.stl").status_code == 404


def test_session_requires_serial_and_operator(client):
    assert "serial" in _error(
        client.client.post("/api/session", json={"operator": "a"})
    )
    assert "operator" in _error(
        client.client.post("/api/session", json={"serial": "DLS0011116"})
    )
    assert "type" in _error(
        client.client.post("/api/session", json={"serial": "UNKNOWN", "operator": "a"})
    )


def test_survey_needs_three_points(client):
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    assert "3 XYZ rows" in _error(
        client.client.post("/api/survey", json={"points": [[0, 0, 0]]})
    )


def test_survey_must_match_the_girder(client, survey_for):
    """A survey of the wrong girder type must be refused, not fitted."""
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    wrong = survey_for(G.machine("LM"), {})
    assert "matched" in _error(
        client.client.post("/api/survey", json={"points": wrong})
    )


def test_full_alignment_workflow(client, survey_for, cfg):
    """Start, survey, walk every step to its target, converge, report."""
    machine = G.machine("MS")
    err = {"roll": 0.4, "pitch": -0.2, "heave": 0.5, "sway": 0.3, "yaw": 0.1}

    started = _json(
        client.client.post(
            "/api/session",
            json={
                "serial": "DLS0011116",
                "operator": "tester",
                "combine_vertical": True,
            },
        )
    )
    assert started["ok"]
    assert started["session"]["gtype"] == "MS"

    surveyed = _json(
        client.client.post("/api/survey", json={"points": survey_for(machine, err)})
    )
    assert surveyed["ok"]
    fit = surveyed["survey"]["fit"]
    assert fit["n"] == len(machine.fids)
    assert fit["rms"] < 1e-6
    assert not surveyed["survey"]["assessment"]["in_tolerance"]

    session = surveyed["session"]
    assert session["phase"] == "moving"
    n_steps = session["n_steps"]
    assert n_steps == 4  # combined vertical (two pairs) + sway + surge

    seen_groups = []
    for i in range(n_steps):
        state = _json(client.client.get("/api/session"))["session"]
        assert state["step_index"] == i
        step = state["step"]

        # First read of a group captures the datum the targets are measured from.
        enc = _json(client.client.get("/api/encoders"))
        assert enc["datum_group"] == step["group"]
        seen_groups.append(step["group"])

        _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
        enc = _settle(client)
        assert enc["check"]["ok"], enc["check"]["reasons"]
        assert abs(enc["parasitic"]["warp"]) < G.WARP_STOP
        assert abs(enc["parasitic"]["stretch"]) < G.STRETCH_STOP

        done = _json(client.client.post("/api/step/complete", json={}))
        assert done["accepted"], done.get("check")
        assert done["move"]["datum_group"] == step["group"]

    assert seen_groups == ["combined", "combined", "horizontal", "horizontal"]
    assert _json(client.client.get("/api/session"))["session"]["phase"] == "verify"

    # A converged re-survey completes the session.
    final = _json(
        client.client.post("/api/survey", json={"points": survey_for(machine, {})})
    )
    assert final["session"]["status"] == "complete"

    report = _json(client.client.post("/api/report", json={"notes": "test run"}))
    assert report["ok"] and not report["partial"]
    assert (cfg.reports / report["pdf"]).stat().st_size > 1000
    assert (cfg.reports / report["json"]).exists()
    assert report["pdf"] in _json(client.client.get("/api/reports"))["reports"]


def test_datum_is_retaken_at_each_group_boundary(client, survey_for):
    """Heave targets assume flatten is applied, so the reference must move."""
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session",
            json={
                "serial": "DLS0011116",
                "operator": "tester",
                "combine_vertical": False,
            },
        )
    )
    _json(
        client.client.post(
            "/api/survey",
            json={"points": survey_for(machine, {"roll": 0.4, "heave": 0.6})},
        )
    )

    datums = {}
    for _ in range(_json(client.client.get("/api/session"))["session"]["n_steps"]):
        step = _json(client.client.get("/api/session"))["session"]["step"]
        _json(client.client.get("/api/encoders"))
        datums.setdefault(step["group"], []).append(dict(client.service.session.datum))
        _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
        _settle(client)
        _json(client.client.post("/api/step/complete", json={}))

    assert list(datums) == ["flatten", "heave", "horizontal"]
    # Within a group the datum is untouched; across groups it moves on.
    for taken in datums.values():
        assert all(d == taken[0] for d in taken)
    assert datums["flatten"][0] != datums["heave"][0]


def test_session_survives_a_restart(client, cfg, survey_for):
    """A pod restart mid-move must resume against the same datum."""
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(G.machine("MS"), {"roll": 0.4})}
        )
    )
    _json(client.client.get("/api/encoders"))
    sid = _json(client.client.get("/api/session"))["session"]["id"]
    datum = dict(client.service.session.datum)
    assert datum

    with Service(cfg, demo=True) as fresh, create_app(fresh).test_client() as c2:
        assert _json(c2.get("/api/session"))["session"] is None
        resumed = _json(c2.post(f"/api/session/resume/{sid}"))
        assert resumed["session"]["id"] == sid
        assert fresh.session is not None
        assert fresh.session.datum == datum
        assert fresh.session.datum_group == "flatten"


def test_stop_can_save_a_partial_report(client, cfg, survey_for):
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(G.machine("MS"), {"heave": 0.5})}
        )
    )
    out = _json(
        client.client.post(
            "/api/session/stop", json={"reason": "end of shift", "report": True}
        )
    )
    assert out["session"]["status"] == "stopped"
    assert (cfg.reports / out["report"]["pdf"]).exists()


def test_close_frees_the_bay(client, survey_for):
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(client.client.post("/api/session/close"))
    assert _json(client.client.get("/api/session"))["session"] is None
    assert (
        _json(client.client.get("/api/sessions"))["sessions"][0]["status"] == "stopped"
    )


# ---------------------------------------------------------------------------
# Backup.
#
# The deployment writes to a PersistentVolumeClaim, which is working storage and
# is not backed up. Reports are pulled off it over HTTP and each carries its own
# session; this endpoint is what covers the sessions that never produced one.
# ---------------------------------------------------------------------------


def test_export_returns_whole_sessions_not_summaries(client, survey_for):
    """A summary would not restore anything. The export has to be the session."""
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(G.machine("MS"), {"heave": 0.5})}
        )
    )
    _json(client.client.post("/api/session/close"))

    out = _json(client.client.get("/api/sessions/export"))
    assert out["count"] == 1
    sid = _json(client.client.get("/api/sessions"))["sessions"][0]["id"]
    session = out["sessions"][sid]
    # The whole thing, round-trippable - not the seven columns /api/sessions has.
    assert session["serial"] == "DLS0011116"
    assert session["plan"]
    assert session["events"]
    assert session["surveys"]


def test_export_matches_what_the_cli_writes(client, survey_for):
    """One implementation, so a backup cannot drift from `sessions export`."""
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(client.client.post("/api/session/close"))
    assert _json(client.client.get("/api/sessions/export"))["sessions"] == (
        client.service.store.export()
    )


def test_export_of_an_empty_store_is_not_an_error(client):
    """A fresh deployment must not make the backup job fail every night."""
    out = _json(client.client.get("/api/sessions/export"))
    assert out["count"] == 0
    assert out["sessions"] == {}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1,2,3", [[1.0, 2.0, 3.0]]),
        ("APNT26,1,2,3", [[1.0, 2.0, 3.0]]),  # name column ignored
        ("1\t2\t3", [[1.0, 2.0, 3.0]]),  # TSV
        ("# comment\n\n1;2;3", [[1.0, 2.0, 3.0]]),  # comments and semicolons
        ("1,2", []),  # too few numerics
    ],
)
def test_survey_parsing(text, expected):
    assert parse_points(text) == expected
