"""Warp (checkerboard jack) correction.

All four girder types now have preliminary FEA compliance data (see
``_fea_data.py``): MS and LM from their own single-jack sweeps, SM and ML by
reusing MS's and LM's numbers respectively (confirmed geometrically
identical jack/venc/fiducial layout within each pair). Every warp code path
must still fall back to exactly the pre-existing rigid-only behaviour for a
girder type absent from FEA_COMPLIANCE - simulated below via monkeypatch,
since there is no longer a real uncharacterised type to exercise that path.
"""

from __future__ import annotations

import numpy as np
import pytest

from dls_deb_girder_alignment import geometry as G


def test_warp_pattern_is_the_rigid_null_space(machine: G.Machine):
    """The geometric warp direction must be unreachable by any rigid plane.

    If this drifts, a "warp correction" would actually just be commanding an
    ordinary heave/pitch/roll move under a different name.
    """
    assert set(machine.warp_pattern) == {b["id"] for b in machine.bearings}
    heights = dict(machine.warp_pattern)
    a, b, c = G._plane_fit(machine, heights)
    for bearing in machine.bearings:
        p = bearing["p"]
        assert abs(heights[bearing["id"]] - (a + b * p[0] + c * p[2])) > 1e-6


def test_warp_pattern_is_a_diagonal_checkerboard(ms: G.Machine):
    """US_IN/DS_OUT and US_OUT/DS_IN are the two diagonals, opposite sign."""
    wp = ms.warp_pattern
    assert wp["J_US_IN"] == pytest.approx(1.0, abs=1e-4)
    assert wp["J_DS_OUT"] == pytest.approx(1.0, abs=1e-4)
    assert wp["J_US_OUT"] == pytest.approx(-1.0, abs=1e-4)
    assert wp["J_DS_IN"] == pytest.approx(-1.0, abs=1e-4)


#: One fiducial and one vencoder known to be present in each type's FEA data.
_REPRESENTATIVE_POINT = {
    "MS": ("APNT26", "V_US_IN"),
    "SM": ("APNT26", "V_US_IN"),  # reuses MS's dict, same point names
    "LM": ("APNT47", "V_DS_IN"),
    "ML": ("APNT47", "V_DS_IN"),  # reuses LM's dict, same point names
}


def test_fea_gain_only_present_for_characterised_types(machine: G.Machine):
    """Every real girder type is characterised now - see the module
    docstring for the uncharacterised-type fallback, tested separately via
    monkeypatch since no real type is left to exercise it.
    """
    fid, venc = _REPRESENTATIVE_POINT[machine.type]
    assert machine.fea is not None
    assert machine.fea_gain("fids", fid) is not None
    assert machine.fea_gain("venc", venc) is not None
    assert machine.fea_gain("fids", "no-such-point") is None


def test_every_type_is_now_fully_characterised(machine: G.Machine):
    """MS's original FEA run was missing the girder-centre fiducial
    (APNT30) and LM's was missing the V_US_IN vencoder; fuller re-exports
    filled both in (gains +0.0008 and +0.5076 respectively - the fiducial
    one essentially the checkerboard mode's own symmetry node). SM/ML reuse
    MS's/LM's dicts, so every one of the four types should now have a gain
    for every one of its own fiducials AND vencoders - no gaps left
    anywhere in FEA_COMPLIANCE (see the module docstring).
    """
    for name in machine.fids:
        assert machine.fea_gain("fids", name) is not None, name
    for e in machine.venc:
        assert machine.fea_gain("venc", e["id"]) is not None, e["id"]


def test_uncharacterised_type_falls_back_to_rigid_only(monkeypatch, survey_for):
    """A girder type absent from FEA_COMPLIANCE must behave exactly as the
    pre-existing rigid-only model - every real type is characterised now
    (see the module docstring), so this simulates "absent" by clearing one
    type's entry rather than relying on a permanently-uncharacterised type.
    """
    monkeypatch.delitem(G.FEA_COMPLIANCE, "LM")
    # G.machine() caches instances across the whole test session - bypass it
    # with a fresh Machine() directly, or this would return the already-
    # constructed (pre-monkeypatch) cached LM with its real FEA data intact.
    machine = G.Machine("LM")
    assert machine.fea is None
    assert machine.fea_gain("fids", "APNT47") is None

    measured = dict(zip(machine.fids, survey_for(machine, {"heave": 0.3}), strict=True))
    fit = G.fit_pose(measured, machine)
    assert fit.warp_modeled is False
    assert fit.err["warp"] == 0.0


def test_fit_recovers_an_injected_warp_shape(machine: G.Machine):
    """A pure warp bend (no rigid pose error) must fit back to itself."""
    injected = 0.15  # mm of warp_pattern-equivalent jack command
    measured = {}
    for name, nom in machine.fids.items():
        p = list(nom)
        gain = machine.fea_gain("fids", name)
        if gain is not None:
            p[1] += injected * gain
        measured[name] = p

    fit = G.fit_pose(measured, machine)
    assert fit.warp_modeled is True
    assert fit.err["warp"] == pytest.approx(injected, abs=1e-6)
    for k in ("sway", "heave", "surge", "roll", "pitch", "yaw"):
        assert fit.err[k] == pytest.approx(0.0, abs=1e-6)
    assert fit.rms < 1e-6


def test_warp_correction_rides_with_heave_not_flatten(machine: G.Machine):
    """Folded into heave/combined (as directed); never into flatten alone."""
    err = {"roll": 0.2, "pitch": -0.1, "heave": 0.4, "warp": 0.08}
    plan = G.compute_plan(machine, err, combine_vertical=False)
    assert plan["flatten"]["warp"] == 0.0
    assert plan["heave"]["warp"] == pytest.approx(0.08)

    combined = G.compute_plan(machine, err, combine_vertical=True)
    assert combined["combined"]["warp"] == pytest.approx(0.08)


def test_warp_correction_actually_offsets_the_jack_targets(machine: G.Machine):
    """A nonzero warp must change the final encoder targets vs. warp=0."""
    base = G.compute_plan(machine, {"heave": 0.4, "warp": 0.0})
    warped = G.compute_plan(machine, {"heave": 0.4, "warp": 0.1})
    diffs = [
        abs(warped["heave"]["second"]["all"][e] - base["heave"]["second"]["all"][e])
        for e in G.VERT_IDS
    ]
    # FEA gains are ~0.35-0.55 mm/mm across the characterised types; 0.1 mm
    # warp is easily visible.
    assert max(diffs) > 0.01
    assert not np.allclose(diffs, diffs[0])  # not a uniform (rigid) shift


def test_gate_measures_deviation_from_expected_not_from_zero(survey_for):
    """A step's own target — including its warp content — must not itself
    trip the parasitic gate.

    check_step compares the reading against THIS STEP's expected parasitic
    content, not against zero — otherwise every warp correction (or even the
    ordinary intermediate-pair state, which has its own small warp content
    from the IN/OUT jacks not sharing an exact Z position) would fail its own
    gate by design.
    """
    from dls_deb_girder_alignment.session import Session

    s = Session(
        serial="DLS0011116", gtype="MS", operator="tester", combine_vertical=True
    )
    machine = G.machine("MS")
    err = {"roll": 0.4, "pitch": -0.2, "heave": 0.5}
    s.add_survey(survey_for(machine, err))
    s.accept_plan()
    step = s.current_step()
    assert step is not None
    readings = {
        eid: {"relative": step["expected"].get(eid, 0.0)} for eid in G.ENCODER_IDS
    }
    chk = s.check_step(readings, step)
    assert chk["ok"], chk["reasons"]
    assert abs(chk["warp"]) < 1e-9
    assert abs(chk["stretch"]) < 1e-9
