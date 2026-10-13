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


def test_info_exposes_each_types_sag_bias_mm(client):
    """The "bare vs loaded" setup toggle looks this up per type so its note
    text can never drift from the actual value compute_plan will use.
    """
    info = _json(client.client.get("/api/info"))
    assert info["types"]["MS"]["sag_bias_mm"] == pytest.approx(0.24)


def test_bare_girder_defaults_to_the_type_bias(client):
    """Omitting heave_bias_mm (the "Bare girder" toggle state) must fall
    back to the type's own recommended pre-set, not zero.
    """
    started = _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    assert started["session"]["heave_bias_mm"] == pytest.approx(0.24)


def test_loaded_girder_forces_zero_bias(client):
    """The "Loaded with magnets" toggle state sends heave_bias_mm=0.0
    explicitly - the girder has already sagged under the real magnet
    weight, so applying the bare-girder pre-set on top would be double
    counting it.
    """
    started = _json(
        client.client.post(
            "/api/session",
            json={
                "serial": "DLS0011116",
                "operator": "tester",
                "heave_bias_mm": 0.0,
            },
        )
    )
    assert started["session"]["heave_bias_mm"] == 0.0


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

    # The plan doesn't exist yet - the operator has to see the pose error and
    # confirm before targets appear.
    session = surveyed["session"]
    assert session["phase"] == "solved"
    assert session["step"] is None

    accepted = _json(client.client.post("/api/plan/accept", json={}))
    assert accepted["ok"]
    session = accepted["session"]
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

    # A converged re-survey — on the session's own target, heave pre-set
    # included — completes the session.
    bias = _json(client.client.get("/api/session"))["session"]["heave_bias_mm"]
    err = {"heave": bias} if bias else {}
    final = _json(
        client.client.post("/api/survey", json={"points": survey_for(machine, err)})
    )
    assert final["session"]["status"] == "complete"

    report = _json(client.client.post("/api/report", json={"notes": "test run"}))
    assert report["ok"] and not report["partial"]
    assert (cfg.reports / report["pdf"]).stat().st_size > 1000
    assert (cfg.reports / report["json"]).exists()
    assert report["pdf"] in _json(client.client.get("/api/reports"))["reports"]


def test_loop_back_survey_reactivates_a_complete_session(client, survey_for):
    """A confirmation survey that comes back OUT of tolerance after an earlier
    one already converged (status "complete") must put the session back into
    play - otherwise the UI's poll loop, which stops once a session looks
    "ended", never restarts and no step or encoder data shows for the
    second iteration at all.
    """
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session",
            json={"serial": "DLS0011116", "operator": "tester", "heave_bias_mm": 0.0},
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(machine, {"heave": 0.5})}
        )
    )
    _json(client.client.post("/api/plan/accept", json={}))
    n_steps = _json(client.client.get("/api/session"))["session"]["n_steps"]
    for _ in range(n_steps):
        _json(client.client.get("/api/encoders"))
        _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
        _settle(client)
        _json(client.client.post("/api/step/complete", json={}))

    converged = _json(
        client.client.post("/api/survey", json={"points": survey_for(machine, {})})
    )
    assert converged["session"]["status"] == "complete"
    # Phase stays "solved" even when converged - the operator can still
    # choose to accept and refine further - so "ended" here comes from
    # status alone, which is exactly what must reset on the next survey.
    assert converged["session"]["phase"] == "solved"

    # A second, out-of-tolerance confirmation survey - e.g. the operator
    # picked the wrong file, or the girder genuinely drifted before it was
    # re-checked - must reopen the session, not leave it looking finished.
    loop_back = _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(machine, {"heave": 0.3})}
        )
    )
    assert loop_back["session"]["status"] == "active"
    assert loop_back["session"]["phase"] == "solved"
    assert not loop_back["survey"]["assessment"]["in_tolerance"]


def test_step_back_within_a_group_reproduces_the_original_target(client, survey_for):
    """/api/step/back must not just replay the targets computed before any
    physical move happened — that was a latent bug: the session-layer recompute
    path existed but nothing on the request path ever fed it live data. This
    exercises the actual HTTP route, not just the session method directly.

    But recomputing FROM the live reading isn't right either when the step
    we're returning to is in the group we're still inside: only one of the
    group's two pair-moves is done, so the girder is in a genuinely
    asymmetric state a single scalar can't describe — see
    Session.estimate_live_pose. The correct answer here is that the
    already-completed step's target comes back exactly as it was (and as it
    was achieved), not some fraction of it.
    """
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session",
            json={"serial": "DLS0011116", "operator": "tester", "heave_bias_mm": 0.0},
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(machine, {"heave": 0.5})}
        )
    )
    _json(client.client.post("/api/plan/accept", json={}))
    assert _json(client.client.get("/api/session"))["session"]["n_steps"] == 4

    original_targets = _json(client.client.get("/api/session"))["session"]["step"][
        "targets"
    ]

    # Complete the first vertical pair fully, so we're clear of step 0.
    _json(client.client.get("/api/encoders"))
    _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
    _settle(client)
    done = _json(client.client.post("/api/step/complete", json={}))
    assert done["accepted"]

    state = _json(client.client.get("/api/session"))["session"]
    assert state["step_index"] == 1

    # Now at step 1 (the second vertical pair): drive PART way there, so the
    # girder has genuinely moved but hasn't reached this step's target either.
    # Driven explicitly by PV, targeting only step 1's own two encoders -
    # "fraction" scales the whole block's expected state including the pair
    # already parked at its step-0 target, which would retract it and
    # confound this test with a second, unrelated simulator quirk.
    enc0 = _json(client.client.get("/api/encoders"))
    step1 = state["step"]
    half_targets = {
        eid: enc0["readings"][eid]["datum"] + 0.5 * delta
        for eid, delta in step1["targets"].items()
    }
    _json(client.client.post("/api/demo/drive", json={"targets": half_targets}))
    for _ in range(50):
        _json(client.client.get("/api/encoders"))

    back = _json(client.client.post("/api/step/back", json={}))
    assert back["ok"]
    assert back["recomputed"], "the live-pose recompute path must actually fire"
    assert back["session"]["step_index"] == 0

    # heave is "combined"'s own axis, so it must come back unchanged - not
    # blended with the still-in-progress second pair's partial reading.
    assert back["session"]["current_err"]["heave"] == pytest.approx(0.5, abs=1e-6)
    for eid, tgt in back["step"]["targets"].items():
        assert tgt == pytest.approx(original_targets[eid], abs=1e-6)

    # The first pair was fully driven to its target before we stepped back
    # into it - recomputing must NOT also recapture the datum (that was the
    # remaining bug: it made an already-achieved pair read back as "at zero,
    # full move still needed", because the fresh datum absorbed the very
    # progress it was supposed to measure).
    enc = _json(client.client.get("/api/encoders"))
    for eid, tgt in back["step"]["targets"].items():
        rel = enc["readings"][eid]["relative"]
        assert rel == pytest.approx(tgt, abs=1e-3), (
            f"{eid}: datum was recaptured on step-back, wiping out progress "
            f"already made within the still-open group (relative={rel}, "
            f"target={tgt})"
        )


def test_step_back_across_a_group_boundary_keeps_earlier_progress(client, survey_for):
    """Stepping back into an EARLIER, different move group must not forget
    the correction already completed there.

    A live reading is only relative to the CURRENT group's datum, so on its
    own it's blind to a previous group's progress — that datum is gone,
    superseded the moment the new group opened. The bug this guards: fully
    complete "combined", move into "sway", then back up into "combined"
    again — a naive live-pose estimate reads ~0 on the (freshly re-datumed)
    vertical encoders and reports the ORIGINAL, unfixed heave error again,
    even though the girder is actually sitting correctly.
    """
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session",
            json={"serial": "DLS0011116", "operator": "tester", "heave_bias_mm": 0.0},
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(machine, {"heave": 0.5})}
        )
    )
    _json(client.client.post("/api/plan/accept", json={}))

    # Complete BOTH vertical-pair steps fully, so the whole "combined" group
    # — and the heave error it corrects — is genuinely done.
    for _ in range(2):
        _json(client.client.get("/api/encoders"))
        _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
        _settle(client)
        done = _json(client.client.post("/api/step/complete", json={}))
        assert done["accepted"]

    state = _json(client.client.get("/api/session"))["session"]
    assert state["step_index"] == 2
    assert state["step"]["group"] == "horizontal"

    back = _json(client.client.post("/api/step/back", json={}))
    assert back["ok"] and back["recomputed"]
    assert back["session"]["step_index"] == 1
    assert back["step"]["group"] == "combined"

    # The estimated pose must show heave as essentially corrected, not back
    # to the original 0.5 mm the survey started at.
    assert abs(back["session"]["current_err"]["heave"]) < 0.02
    # And the re-solved targets for this (already-completed) step must be
    # small — settling residue, not the original large uncorrected move.
    for tgt in back["step"]["targets"].values():
        assert abs(tgt) < 0.02


def test_step_back_within_separate_heave_group_does_not_undershoot(client, survey_for):
    """The real-world report this guards: with flatten/heave run SEPARATELY
    (not combined), heave's own plan is a pure, uniform Y shift with no
    differential — it can't express "first pair done, second pair still
    needs to move". Once heave_first is complete and heave_second is still
    open, the girder is asymmetric (one pair moved, the other hasn't); a full
    rigid fit of that state misattributes part of the asymmetry as apparent
    pitch, which silently steals from the estimated heave value and produces
    a target well short of what heave_first already, correctly, achieved.
    """
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session",
            json={
                "serial": "DLS0011116",
                "operator": "tester",
                "heave_bias_mm": 0.0,
                "combine_vertical": False,
            },
        )
    )
    _json(
        client.client.post(
            "/api/survey",
            json={"points": survey_for(machine, {"roll": 0.3, "heave": 0.5})},
        )
    )
    _json(client.client.post("/api/plan/accept", json={}))

    # Complete both flatten steps, then heave_first only - so heave (a
    # separate group here) is genuinely mid-sequence: one pair done, one not.
    for _ in range(2):
        _json(client.client.get("/api/encoders"))
        _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
        _settle(client)
        assert _json(client.client.post("/api/step/complete", json={}))["accepted"]

    heave_first_target = _json(client.client.get("/api/session"))["session"]["step"][
        "targets"
    ]
    _json(client.client.get("/api/encoders"))
    _json(client.client.post("/api/demo/drive", json={"fraction": 1.0}))
    _settle(client)
    done = _json(client.client.post("/api/step/complete", json={}))
    assert done["accepted"]
    achieved = done["move"]["achieved"]
    for _ in range(5):
        _json(client.client.get("/api/encoders"))

    back = _json(client.client.post("/api/step/back", json={}))
    assert back["ok"] and back["recomputed"]
    assert back["step"]["group"] == "heave"

    # The recomputed target for the already-completed pair must land back
    # near what was actually achieved - not roughly half of it.
    for eid, tgt in back["step"]["targets"].items():
        assert tgt == pytest.approx(heave_first_target[eid], abs=0.01)
        assert tgt == pytest.approx(achieved[eid], abs=0.02)


def test_step_back_at_the_first_step_is_a_clean_no_op(client, survey_for):
    machine = G.machine("MS")
    _json(
        client.client.post(
            "/api/session", json={"serial": "DLS0011116", "operator": "tester"}
        )
    )
    _json(
        client.client.post(
            "/api/survey", json={"points": survey_for(machine, {"heave": 0.5})}
        )
    )
    res = _json(client.client.post("/api/step/back", json={}))
    assert res["ok"] is False


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
    _json(client.client.post("/api/plan/accept", json={}))

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
    _json(client.client.post("/api/plan/accept", json={}))
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
        assert fresh.session.datum_group == "combined"  # combine_vertical defaults on


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
    _json(client.client.post("/api/plan/accept", json={}))
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
