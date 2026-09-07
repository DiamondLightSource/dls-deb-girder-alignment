"""Tests for the authoritative alignment maths.

These lock down the invariants the module's own docstrings claim, because those
claims are what the rest of the tool is built on.
"""

from __future__ import annotations

import numpy as np
import pytest

from dls_deb_girder_alignment import geometry as G


def test_all_girder_types_load(machine: G.Machine):
    assert machine.J.shape == (8, 6)
    assert len(machine.fids) >= 3


def test_jacobian_is_full_rank(machine: G.Machine):
    """Six degrees of freedom must all be observable from eight encoders."""
    assert machine.rank() == 6


def test_parasitic_modes_are_the_left_null_space(machine: G.Machine):
    """WARP and STRETCH are exactly the encoder patterns a rigid girder cannot make.

    If this drifts, the parasitic monitors stop meaning "non-rigid" and start
    rejecting perfectly good moves.
    """
    assert machine.parasitic.shape == (2, 8)
    assert np.allclose(machine.parasitic @ machine.J, 0.0, atol=1e-9)


def test_rigid_motion_shows_no_parasitic_content(machine: G.Machine):
    """Any pose the girder can physically take must read as zero warp/stretch."""
    deltas = machine.encoder_deltas(
        {
            "roll": 0.3,
            "pitch": -0.2,
            "yaw": 0.1,
            "sway": 0.5,
            "heave": -0.4,
            "surge": 0.2,
        }
    )
    warp, stretch = machine.parasitic_of([deltas[e] for e in G.ENCODER_IDS])
    assert abs(warp) < 1e-9
    assert abs(stretch) < 1e-9


def test_cross_shift_ratio_is_about_five_percent(machine: G.Machine):
    """The ~0.049 mm/mm figure the whole sequential-pair scheme exists for."""
    assert 0.03 < G.cross_shift_ratio(machine) < 0.07


def test_labels_are_derived_from_position(machine: G.Machine):
    """IN/OUT and US/DS are computed, never hand-labelled - a past source of bugs."""
    ids = [b["id"] for b in machine.bearings]
    assert ids == ["J_US_IN", "J_US_OUT", "J_DS_IN", "J_DS_OUT"]
    along = [float((b["p"] - machine.p0) @ machine.jz) for b in machine.bearings]
    assert along[0] < 0 and along[2] > 0  # US upstream of DS


@pytest.mark.parametrize(
    "err",
    [
        {"heave": 1.0},
        {"roll": 0.5},
        {"sway": -0.8, "yaw": 0.3},
        {
            "roll": 0.2,
            "pitch": -0.15,
            "heave": 0.6,
            "sway": 0.4,
            "surge": -0.3,
            "yaw": 0.1,
        },
    ],
)
def test_fit_recovers_a_known_pose(ms: G.Machine, err: dict, survey_for):
    """A survey generated from a known pose must fit back to that pose."""
    pts = survey_for(ms, err)
    matched, unmatched = G.match_by_proximity(pts, ms)
    assert not unmatched
    assert len(matched) == len(ms.fids)

    fit = G.fit_pose(matched, ms)
    for key in G.AXIS_KEYS:
        assert fit.err[key] == pytest.approx(err.get(key, 0.0), abs=1e-6)
    assert fit.rms < 1e-9


def test_survey_points_match_by_position_not_name(ms: G.Machine, survey_for):
    """Names repeat between girder types, so order must not matter."""
    pts = survey_for(ms, {"heave": 0.5})
    shuffled = list(reversed(pts))
    a = G.fit_pose(G.match_by_proximity(pts, ms)[0], ms)
    b = G.fit_pose(G.match_by_proximity(shuffled, ms)[0], ms)
    assert a.err["heave"] == pytest.approx(b.err["heave"])


def test_far_survey_points_do_not_match(ms: G.Machine, survey_for):
    pts = survey_for(ms, {"heave": 2 * G.PROXIMITY_GATE_MM})
    matched, unmatched = G.match_by_proximity(pts, ms)
    assert not matched
    assert len(unmatched) == len(pts)


def test_combined_vertical_equals_flatten_plus_heave(ms: G.Machine):
    """The documented invariant that lets the two vertical passes be merged."""
    err = {"roll": 0.4, "pitch": -0.2, "heave": 0.7}
    plan = G.compute_plan(ms, err)
    for eid in G.VERT_IDS:
        combined = plan["combined"]["second"]["all"][eid]
        summed = (
            plan["flatten"]["second"]["all"][eid] + plan["heave"]["second"]["all"][eid]
        )
        assert combined == pytest.approx(summed, abs=1e-9)


def test_sway_and_surge_are_decoupled(ms: G.Machine):
    """Yaw comes purely from differential sway and must not move surge encoders.

    This is the consequence of using the bearing-pair midpoint as the stage
    lever rather than the encoder's own position.
    """
    deltas = ms.encoder_deltas({"yaw": 1.0, "sway": 0.5})
    # Zero to rounding: 1 mrad of yaw leaks well under a nanometre into surge,
    # against a 0.001 mm encoder resolution.
    assert deltas["SURGE_US"] == pytest.approx(0.0, abs=1e-6)
    assert deltas["SURGE_DS"] == pytest.approx(0.0, abs=1e-6)


def test_only_the_last_pair_lands_on_final_targets(ms: G.Machine):
    """The first pair stops short, by the cross-shift the second pair will add."""
    plan = G.compute_plan(ms, {"roll": 0.5, "pitch": 0.2})
    first = plan["flatten"]["first"]
    assert first["intermediate"] != first["final"]
    for inter, final in zip(first["intermediate"], first["final"], strict=True):
        assert inter != pytest.approx(final, abs=1e-6)


def test_step_sequence_and_grouping(ms: G.Machine):
    """Vertical before horizontal, and each step tagged with its datum group."""
    plan = G.compute_plan(ms, {"roll": 0.3, "heave": 0.5, "sway": 0.4})
    groups = [s["group"] for s in plan["steps"]]
    assert groups == ["flatten"] * 2 + ["heave"] * 2 + ["horizontal"] * 2

    combined = G.compute_plan(ms, {"roll": 0.3, "heave": 0.5}, combine_vertical=True)
    assert [s["group"] for s in combined["steps"]] == ["combined"] * 2 + [
        "horizontal"
    ] * 2

    for step in plan["steps"]:
        assert set(step["targets"]) | set(step["monitors"]) == set(G.ENCODER_IDS)


def test_horizontal_steps_expect_zero_vertical(ms: G.Machine):
    """The horizontal steps open a new datum group, so the verticals read zero.

    A stage only translates, so any vertical excursion during a horizontal move
    means the girder is riding a spherical bearing - which is what the monitors
    are there to catch.
    """
    plan = G.compute_plan(ms, {"roll": 0.3, "heave": 0.5, "sway": 0.4})
    for step in plan["steps"]:
        if step["group"] == "horizontal":
            for vid in G.VERT_IDS:
                assert step["expected"][vid] == 0.0


def test_predicted_pose_change_inverts_the_jacobian(ms: G.Machine):
    """Achieved encoder deltas must map back to the pose that produced them."""
    err = {"roll": 0.3, "heave": 0.4, "sway": -0.2, "yaw": 0.1}
    achieved = ms.encoder_deltas(err)
    back = G.predicted_pose_change(ms, achieved)
    for key, want in err.items():
        assert back[key] == pytest.approx(-want, abs=1e-6)


def test_assess_flags_out_of_tolerance():
    tol = G.tolerances()
    good = G.assess(dict.fromkeys(G.AXIS_KEYS, 0.0), tol)
    assert good["in_tolerance"]
    bad = G.assess({"roll": 10 * tol["roll"]}, tol)
    assert not bad["in_tolerance"]
    assert bad["axes"]["roll"]["status"] == "bad"


def test_fit_warns_on_a_bad_point(ms: G.Machine, survey_for):
    pts = survey_for(ms, {"heave": 0.5})
    pts[0] = [pts[0][0], pts[0][1] + 1.0, pts[0][2]]  # nudge one fiducial 1 mm
    fit = G.fit_pose(G.match_by_proximity(pts, ms)[0], ms)
    assert fit.warnings
    assert fit.rms > 0.1
