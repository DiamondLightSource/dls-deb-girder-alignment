"""Bare-girder heave pre-set (see _fea_data.SAG_BIAS_MM).

Magnets are aligned to nominal individually, after the girder is fully
loaded, so this doesn't change any magnet's final position — only how much
travel its adjuster needs, and in which direction. MS's bare-vs-loaded FEA
run is the only one in use: a deliberate decision (2026-10-12) applies its
+0.24 mm bias to all four girder types uniformly, rather than each type
using its own run (LM had one too, +0.113 mm, since scrapped in favour of
this single shared value; SM/ML never had their own).
"""

from __future__ import annotations

import pytest

from dls_deb_girder_alignment import geometry as G
from dls_deb_girder_alignment.session import Session

#: All four types now share MS's bias by deliberate policy, not per-type FEA.
_EXPECTED_BIAS = {"MS": 0.24, "SM": 0.24, "LM": 0.24, "ML": 0.24}


def test_default_bias_is_the_same_for_every_type(machine: G.Machine):
    assert machine.sag_bias_mm == pytest.approx(_EXPECTED_BIAS[machine.type])


def test_compute_plan_defaults_to_the_machine_bias(ms: G.Machine):
    plan = G.compute_plan(ms, {"heave": 0.0})
    assert plan["heave_bias_mm"] == pytest.approx(0.24)
    # Correction commanded = err["heave"] - bias = 0.0 - 0.24 = -0.24: the
    # girder is driven UP by 0.24 mm even though the survey showed zero error.
    assert plan["heave"]["first"]["all"]["V_US_IN"] < 0


def test_explicit_bias_overrides_and_can_be_disabled(ms: G.Machine):
    zeroed = G.compute_plan(ms, {"heave": 0.0}, heave_bias_mm=0.0)
    assert zeroed["heave_bias_mm"] == 0.0
    assert zeroed["heave"]["first"]["all"]["V_US_IN"] == pytest.approx(0.0)

    custom = G.compute_plan(ms, {"heave": 0.0}, heave_bias_mm=0.5)
    assert custom["heave_bias_mm"] == pytest.approx(0.5)


def test_compute_plan_default_bias_matches_the_machine(machine: G.Machine):
    plan = G.compute_plan(machine, {"heave": 0.0})
    assert plan["heave_bias_mm"] == pytest.approx(_EXPECTED_BIAS[machine.type])


def test_assess_treats_the_bias_as_on_target():
    """Sitting exactly on the biased target must read 'ok', not 'bad'."""
    tol = G.tolerances()
    on_target = G.assess({"heave": 0.24}, tol, target={"heave": 0.24})
    assert on_target["axes"]["heave"]["status"] == "ok"
    assert on_target["in_tolerance"]

    # Without the target (old behaviour / other axes), the same raw value
    # reads as badly out of tolerance — confirms the fix is doing something.
    unbiased = G.assess({"heave": 0.24}, tol)
    assert unbiased["axes"]["heave"]["status"] == "bad"

    # Raw true-nominal is now the thing that's off target.
    at_zero = G.assess({"heave": 0.0}, tol, target={"heave": 0.24})
    assert at_zero["axes"]["heave"]["status"] == "bad"


def test_session_uses_the_bias_by_default(survey_for):
    s = Session(serial="DLS0011116", gtype="MS", operator="tester")
    assert s.heave_bias_mm == pytest.approx(0.24)

    machine = G.machine("MS")
    s.add_survey(survey_for(machine, {}))  # girder surveyed at TRUE nominal
    # True nominal is short of the +0.24 target, so this must not read complete.
    assert s.phase == "solved"
    s.accept_plan()
    assert s.plan is not None
    assert s.plan["heave_bias_mm"] == pytest.approx(0.24)

    # Re-survey at the actual (biased) target completes the session.
    s.add_survey(survey_for(machine, {"heave": s.heave_bias_mm}))
    assert s.phase == "solved"
    assert s.status == "complete"


def test_session_can_opt_out_of_the_bias(survey_for):
    s = Session(serial="DLS0011116", gtype="MS", operator="tester", heave_bias_mm=0.0)
    assert s.heave_bias_mm == 0.0
    machine = G.machine("MS")
    s.add_survey(survey_for(machine, {}))
    assert s.phase == "solved"
    assert s.status == "complete"
