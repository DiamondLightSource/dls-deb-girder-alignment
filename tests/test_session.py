"""Session state machine, the plan datum, gating and persistence."""

from __future__ import annotations

import pytest

from dls_deb_girder_alignment import geometry as G
from dls_deb_girder_alignment.session import Session, SessionStore


def _step(session: Session) -> dict:
    """The active step, asserted present so the tests stay readable."""
    step = session.current_step()
    assert step is not None
    return step


def _readings(values: dict[str, float]) -> dict[str, dict]:
    """Fake encoder readings already measured from the datum."""
    return {
        eid: {
            "encoder": eid,
            "relative": values.get(eid, 0.0),
            "absolute": values.get(eid, 0.0),
        }
        for eid in G.ENCODER_IDS
    }


@pytest.fixture
def session(survey_for) -> Session:
    s = Session(serial="DLS0011116", gtype="MS", operator="tester", bay="1")
    s.add_survey(survey_for(G.machine("MS"), {"roll": 0.5, "heave": 0.4, "sway": 0.3}))
    s.accept_plan()  # phase pauses at "solved" until the operator confirms
    return s


def test_survey_pauses_for_accept_before_building_a_plan(survey_for):
    """A fit out of tolerance must not silently start commanding targets —
    the operator sees the pose error and confirms before any plan exists.
    """
    s = Session(serial="DLS0011116", gtype="MS", operator="tester", bay="1")
    s.add_survey(survey_for(G.machine("MS"), {"roll": 0.5, "heave": 0.4, "sway": 0.3}))
    assert s.phase == "solved"
    assert s.plan is None
    assert s.current_step() is None

    s.accept_plan()
    assert s.phase == "moving"
    assert s.plan is not None
    assert _step(s)["group"] == "combined"  # combine_vertical defaults on


def test_accept_plan_refuses_outside_the_solved_phase(survey_for):
    s = Session(serial="DLS0011116", gtype="MS", operator="tester", bay="1")
    with pytest.raises(RuntimeError):
        s.accept_plan()  # await_survey - nothing to accept yet

    # On the session's own target (heave pre-set included) the survey is
    # converged (status "complete"), but phase stays "solved" either way -
    # the operator can still choose to accept and refine further even
    # though nothing forces them to.
    err = {"heave": s.heave_bias_mm} if s.heave_bias_mm else {}
    s.add_survey(survey_for(G.machine("MS"), err))
    assert s.phase == "solved"
    assert s.status == "complete"
    s.accept_plan()
    assert s.phase == "moving"
    assert s.status == "active"  # building a plan reopens a "complete" session
    with pytest.raises(RuntimeError):
        s.accept_plan()  # already moving, not "solved" any more


def test_datum_is_needed_once_per_group(session: Session):
    """The datum is re-taken at group boundaries and nowhere else."""
    step = _step(session)
    assert session.needs_datum(step)
    session.capture_datum(dict.fromkeys(G.ENCODER_IDS, 1.5), step["group"])
    assert not session.needs_datum(step)

    # Second step of the same group reuses it...
    session.step_index = 1
    assert not session.needs_datum(_step(session))
    # ...and the next group does not.
    session.step_index = 2
    assert session.needs_datum(_step(session))


def test_relative_readings_are_measured_from_the_datum(session: Session):
    """This is what makes a delta target mean anything."""
    session.capture_datum(dict.fromkeys(G.ENCODER_IDS, 2.0), "flatten")
    assert session.datum["V_US_IN"] == 2.0
    assert session.datum_group == "flatten"


def test_a_new_plan_invalidates_the_datum(session: Session):
    session.capture_datum(dict.fromkeys(G.ENCODER_IDS, 1.0), "flatten")
    session.solve()
    assert session.datum == {}
    assert session.datum_group == ""


def test_gate_blocks_a_step_that_is_off_target(session: Session):
    step = _step(session)
    chk = session.check_step(_readings({}), step)
    assert not chk["ok"]
    assert any("off target" in r for r in chk["reasons"])


def test_gate_passes_on_target(session: Session):
    step = _step(session)
    chk = session.check_step(_readings(step["expected"]), step)
    assert chk["ok"], chk["reasons"]


def test_gate_ignores_the_age_of_a_reading(session: Session):
    """A static encoder is the normal case, not a fault.

    These records only process when the PLC value changes, so a girder that has
    stopped moving carries an old timestamp on every encoder. Gating on age
    refused to pass a step exactly when it had been completed.
    """
    step = _step(session)
    r = _readings(step["expected"])
    for d in r.values():
        d["age"] = 3600.0
    assert session.check_step(r, step)["ok"]


def test_gate_rejects_a_disconnected_encoder(session: Session):
    """No value at all is a different thing, and still blocks the step."""
    step = _step(session)
    r = _readings(step["expected"])
    r["V_US_IN"]["relative"] = None
    r["V_US_IN"]["absolute"] = None
    chk = session.check_step(r, step)
    assert not chk["ok"]
    assert any("V_US_IN" in reason for reason in chk["reasons"])


def test_gate_rejects_a_parasitic_excursion(session: Session):
    """On target with WARP out of limit is not a step to wave through."""
    step = _step(session)
    # Build the excursion from the actual warp mode. An arbitrary perturbation
    # would land mostly in the rigid subspace and simply read as a pose change.
    warp_mode = session.machine.parasitic[0]
    warped = {
        eid: step["expected"][eid] + 10 * G.WARP_STOP * float(warp_mode[i])
        for i, eid in enumerate(G.ENCODER_IDS)
    }
    chk = session.check_step(_readings(warped), step)
    assert not chk["ok"]
    assert not chk["parasitic_ok"]


def test_complete_step_records_an_optional_comment(session: Session):
    step = _step(session)
    session.complete_step(_readings(step["expected"]), comment="  looked fine  ")
    assert session.moves[-1]["comment"] == "looked fine"


def test_complete_step_comment_defaults_to_empty(session: Session):
    step = _step(session)
    session.complete_step(_readings(step["expected"]))
    assert session.moves[-1]["comment"] == ""
    assert session.moves[-1]["reopened"] is False


def test_step_back_flags_the_reopened_step_as_such(session: Session):
    """A step already confirmed complete, then stepped back into, must be
    flagged on its move record - the report highlights this so a technician
    reading it later doesn't mistake the earlier (superseded) entry for the
    final outcome of that step.
    """
    step0 = _step(session)
    session.complete_step(_readings(step0["expected"]))
    assert session.moves[-1]["reopened"] is False

    step1 = _step(session)
    session.complete_step(_readings(step1["expected"]))
    assert session.moves[-1]["reopened"] is False

    res = session.step_back()
    assert res["ok"]
    # The move for the step we just stepped back INTO (step1) is now flagged
    # - step0's move, further back, is untouched.
    assert session.moves[0]["reopened"] is False
    assert session.moves[1]["reopened"] is True


def test_model_validation_accumulates_every_step_since_the_last_survey(
    session: Session, survey_for
):
    """The predicted-vs-achieved model check must sum EVERY step completed
    since the previous survey, not just the last one.

    The session fixture starts with heave off by 0.4mm, corrected by the
    "combined" group's two vertical pairs (steps 1-2 of 4); the round's
    LAST step is "surge" (step 4), which predicts ~0 change in heave of its
    own. Comparing the whole actual heave change against only that last
    step's prediction blamed the whole 0.4mm correction on "model
    residual" - it was really just not the step that made it.
    """
    n_steps = len(session.steps)
    assert n_steps > 1
    for _ in range(n_steps):
        step = _step(session)
        # Normally the server captures this on every /api/encoders read;
        # done by hand here since this test drives the session directly.
        # Without it every move's datum_group is "" and they all collapse
        # into one (wrong) group for the last-move-per-group logic below.
        if session.needs_datum(step):
            session.capture_datum(dict.fromkeys(G.ENCODER_IDS, 0.0), step["group"])
        session.complete_step(_readings(step["expected"]))
    assert session.phase == "verify"

    # The plan only ever commands heave down to the session's own bias
    # target (see compute_plan's heave_bias_mm), not to raw zero - so a
    # self-consistent "after" survey sits at that bias, not at true nominal.
    bias = session.heave_bias_mm or 0.0
    expected_change = bias - 0.4
    session.add_survey(survey_for(G.machine("MS"), {"heave": bias}))
    mc = session.moves[-1].get("model_check")
    assert mc is not None
    assert len(mc["steps"]) == n_steps  # every step in the round, not just one
    assert mc["actual"]["heave"] == pytest.approx(expected_change, abs=1e-3)
    # Fixed: predicted combines the whole round (last move per group, since
    # moves sharing a datum are cumulative, not incremental - see the
    # comment in add_survey), so it tracks the actual heave correction -
    # not just whatever the round's last step (surge) contributed alone.
    assert mc["predicted"]["heave"] == pytest.approx(expected_change, abs=0.05)
    assert abs(mc["residual"]["heave"]) < 0.05


def test_missing_reading_is_json_safe(session: Session):
    """float('inf') here once broke the browser's whole poll loop."""
    step = _step(session)
    r = _readings(step["expected"])
    r["V_US_IN"]["relative"] = None
    chk = session.check_step(r, step)
    assert chk["missing"] and chk["worst"] is None
    assert any("no reading" in x for x in chk["reasons"])


def test_override_is_recorded_with_who_and_why(session: Session):
    step = _step(session)
    res = session.complete_step(
        _readings({}), override=True, reason="jack at end of travel", operator="Sam"
    )
    assert res["accepted"]
    assert len(session.overrides) == 1
    assert session.overrides[0]["reason"] == "jack at end of travel"
    assert session.moves[0]["override_by"] == "Sam"
    assert session.moves[0]["step"] == step["id"]


def test_step_back_without_a_live_pose_just_warns(session: Session):
    """The session layer itself has no live readings - that's server.py's job
    (see test_server.py's step-back tests for the actual recompute path).
    Called with no live_err, it must not silently replay stale targets as if
    they were fresh - it has to say so.
    """
    session.complete_step(_readings(_step(session)["expected"]))
    res = session.step_back()
    assert res["ok"] and not res["recomputed"]
    assert "re-survey" in res["warning"]


def test_step_back_with_a_live_pose_recomputes_the_plan(session: Session):
    before_targets = dict(_step(session)["targets"])
    session.complete_step(_readings(_step(session)["expected"]))
    assert session.current_err is not None
    live_err = dict(session.current_err)
    live_err["heave"] += 0.2  # pretend the girder is 0.2 mm off from the plan
    res = session.step_back(live_err=live_err)
    assert res["ok"] and res["recomputed"]
    assert session.current_err is not None
    assert session.current_err["heave"] == pytest.approx(live_err["heave"])
    assert _step(session)["targets"] != before_targets


def test_undo_survey_restores_the_previous_state(session: Session, survey_for):
    session.add_survey(survey_for(G.machine("MS"), {"heave": 0.1}))
    assert session.iteration == 2
    res = session.undo_survey()
    assert res["ok"]
    assert session.iteration == 1
    assert session.phase == "await_survey"
    assert session.datum_group == ""


def test_converged_survey_completes_the_session(session: Session, survey_for):
    """ "Converged" means on the session's own target, heave pre-set included.

    Phase stays "solved", not "complete" - the operator can still choose to
    accept the survey and generate a further round of correction steps even
    though the session is already marked complete.
    """
    err = {"heave": session.heave_bias_mm} if session.heave_bias_mm else {}
    session.add_survey(survey_for(G.machine("MS"), err))
    assert session.status == "complete"
    assert session.phase == "solved"


def test_persistence_round_trip(session: Session, tmp_path):
    """A pod restart mid-move must resume against the same datum."""
    session.capture_datum(dict.fromkeys(G.ENCODER_IDS, 0.25), "flatten")
    with SessionStore(tmp_path / "s.sqlite") as store:
        store.save(session)

        blob = store.load(session.id)
        assert blob is not None
        restored = Session.from_dict(blob)
        assert restored.datum == session.datum
        assert restored.datum_group == "flatten"
        assert restored.step_index == session.step_index
        assert restored.current_err == session.current_err
        assert [m["step"] for m in restored.moves] == [m["step"] for m in session.moves]
        assert store.list()[0]["serial"] == "DLS0011116"
