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
    return s


def test_survey_starts_a_plan(session: Session):
    assert session.phase == "moving"
    assert session.plan is not None
    assert _step(session)["group"] == "flatten"


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


def test_step_back_does_not_replay_stale_targets(session: Session):
    session.complete_step(_readings(_step(session)["expected"]))
    res = session.step_back()
    assert res["ok"] and not res["recomputed"]
    assert "re-survey" in res["warning"]


def test_undo_survey_restores_the_previous_state(session: Session, survey_for):
    session.add_survey(survey_for(G.machine("MS"), {"heave": 0.1}))
    assert session.iteration == 2
    res = session.undo_survey()
    assert res["ok"]
    assert session.iteration == 1
    assert session.phase == "await_survey"
    assert session.datum_group == ""


def test_converged_survey_completes_the_session(session: Session, survey_for):
    session.add_survey(survey_for(G.machine("MS"), {}))
    assert session.status == "complete"
    assert session.phase == "complete"


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
