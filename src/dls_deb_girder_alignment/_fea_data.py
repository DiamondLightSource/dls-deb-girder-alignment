"""
FEA-derived vertical (jack) warp compliance, per girder type.

Four jacks give exactly three rigid vertical DOF (heave, pitch, roll); the
fourth jack combination — the diagonal "warp" checkerboard, geometrically the
null space of the rigid plane fit through the jacks (see
``Machine._warp_pattern``) — is NOT rigid. Preliminary single-jack influence
FEA (±0.2 / ±0.8 mm sweeps per jack) shows the girder genuinely bends in that
mode — roughly 0.7-1.0 mm of real shape change at the survey points, and
roughly 0.35-0.55 mm at the vertical encoders, per mm of warp-pattern jack
command — rather than racking as a rigid model would predict. See the project
memory / design conversation for the full FEA write-up.

Each value below is the point's response (mm of Y shape-change) to ONE mm of
``Machine.warp_pattern`` commanded at the jacks — i.e. the FEA data already
projected onto the geometric warp direction, so it is a single scalar gain per
point, not a matrix. This is exact for a full 4-jack warp command AND for any
partial (e.g. single-pair) jack state, because the rigid-plane subspace and
the warp direction are orthogonal complements of the 4-jack space: any jack
state decomposes uniquely into (rigid part) + (warp_amount * warp_pattern),
and the response is linear in each part separately.

Derivation (all sweeps): each of the 4 jacks was pushed alone by
±0.2 mm and ±0.8 mm (16 single-jack FEA runs), plus a baseline and two pure
rigid-heave runs used only as a sanity check (a rigid heave must decompose to
zero warp amplitude - confirmed to float precision on every girder). For a
single-jack push, the response at any point is overwhelmingly near-field
rigid-ish content plus a genuine but partial warp component - dividing the
raw response by that one push's own (small) warp amplitude is NOT a stable
per-point gain (the two vary independently). The stable, physically-correct
quantity instead comes from superposition: since ``warp_pattern`` is itself
defined as ``sum_k n[k] * (unit push on jack k)``, and the FEA response is
linear, ``response to 1 mm of warp_pattern = sum_k n[k] * (point's own
measured response per mm of jack k alone)``. Each of the 4 per-jack rates
was itself averaged over its own ±0.2/±0.8 mm runs (their spread was
0.00001-0.0005 mm, i.e. effectively perfectly linear) before combining.
Reconstructing a fiducial exactly on the girder's geometric centre this way
gives a gain near zero on both girders - MS's APNT30 comes out at +0.0008,
LM's APNT55 at +0.0341 - essentially the node of the checkerboard mode, as
expected by symmetry, and a useful sanity check that this derivation, not
the naive single-run-residual one, is the right one.

MS was characterised first (prelim-2026-09-15, `APNT30` missing from that
run); re-run 2026-09-20 (`all_points_deformed_coords_FINE3.csv`) with the
same 4 jacks/points/load-steps PLUS the previously-missing centre fiducial
(`SURV_9` -> `APNT30`) - every other point's gain matched the original run
to 4 decimal places, confirming it's the same underlying FEA, just a fuller
export. LM was characterised the same way on 2026-09-20
(`LM_all_points_deformed_coords_FINE2.csv`), also missing one point at
first (`V_US_IN`); a corrected re-export under the SAME filename, same day,
filled it in (`ENC_4` -> `V_US_IN`, gain +0.5076) - again every other point
matched the first export exactly, confirming the same run. SM and ML were
NOT run through their own FEA - they reuse MS's and LM's numbers
respectively, because SM/MS and ML/LM share the identical jack, vertical-
encoder AND fiducial layout (confirmed geometrically: every bearing, venc
and fiducial's position in the *jack frame* matches its counterpart to
sub-micron precision - only the frame's own offset/rotation into the master
frame differs between the two members of each pair, which is already
handled elsewhere in this tool). Point NAMES correspond 1:1 within each
pair (MS's APNT26 is SM's APNT26 at the same jack-frame position, etc.), so
the gain dicts below are reused directly, keyed the same way.

All four types are now fully characterised - every fiducial and every
vencoder has a gain for all of MS/SM/LM/ML. No points remain missing from
FEA_COMPLIANCE (only SAG_BIAS_MM, a different FEA run entirely, still has
gaps - see below).

Horizontal (surge/sway) actuation has negligible effect on this warp mode and
is not modelled here; STRETCH (the horizontal parasitic mode) is corrected via
the surge adjusters, not the vertical jacks, and is unaffected by this data.

SAG_BIAS_MM is a different FEA run: a bare-girder-under-self-weight vs.
fully-loaded-with-magnets comparison ("ASSY1"/"ASSY"), not a jack sweep, and
NOT the same points as the warp compliance above - these are magnet mounting
points ("PM_*"), not survey fiducials or encoders. Magnets are aligned to
nominal individually, AFTER the girder is fully loaded, so this does not
change where any magnet ends up -- it only changes how much travel each
magnet's own fine adjuster has to use, and in which direction, since the
girder itself is never touched again after this tool's own alignment.

For the one characterised run, the bare (self-weight) dataset is the FIRST
load step and the fully-loaded dataset is the LAST; every mounting point's
Y delta between the two is computed, and the bias is the exact minimax
across all of them: -(max + min) / 2, which equalises the worst-case
required magnet correction between whichever point sags least and whichever
sags most, rather than leaving the full asymmetric sag on one side.

MS (prelim-2026-09-15): fully loaded, the girder sags in one smooth hump:
~-0.535 mm at the centre, ~+0.056 mm at the D/S end (the ends rise slightly,
as an overhung beam does). bias = -(0.056 + (-0.535)) / 2 = +0.239 mm ->
+0.24, equalising the worst-case required magnet correction at the centre
and at the ends (+/-0.30 mm each, vs +0.535 mm at the centre alone with no
bias).

**Decision (2026-10-12): MS's bias is applied to all four types.** LM had
its own bare-vs-loaded run too (2026-09-20,
`LM_all_points_deformed_coords_ASSY.csv`, giving +0.113 mm by the same
minimax derivation - preserved in project memory/git history if it's ever
needed again) and SM/ML initially had none pending their own runs, but the
decision was made to scrap the per-type approach: every type now uses MS's
+0.24 mm, not its own FEA-derived figure. Unlike the warp compliance data
above (which genuinely does vary, and was deliberately NOT shared between
LM and MS), this single value is now used uniformly on purpose, not as a
"not characterised yet" fallback.
"""

from __future__ import annotations

FEA_REVISION = "prelim-2026-09-20"

SAG_BIAS_MM: dict[str, float] = {
    "MS": 0.24,
    "SM": 0.24,
    "LM": 0.24,
    "ML": 0.24,
}

_MS_COMPLIANCE = {
    "fids": {
        "APNT34": -0.8499,
        "APNT33": 0.6296,
        "APNT32": 0.6464,
        "APNT31": -0.6383,
        "APNT28": 0.6490,
        "APNT26": 0.8623,
        "APNT27": -0.6352,
        "APNT29": -0.6539,
        "APNT30": 0.0008,  # girder centre — the checkerboard's own node
    },
    "venc": {
        "V_DS_OUT": 0.3569,
        "V_DS_IN": -0.3476,
        "V_US_OUT": -0.3618,
        "V_US_IN": 0.3552,
    },
}

_LM_COMPLIANCE = {
    "fids": {
        "APNT47": 0.9861,
        "APNT48": -0.7316,
        "APNT49": -0.6814,
        "APNT50": 0.7125,
        "APNT51": -0.7259,
        "APNT52": 0.7520,
        "APNT53": 0.7897,
        "APNT54": -1.0033,
        "APNT55": 0.0341,  # girder centre — near zero, the checkerboard's own node
    },
    "venc": {
        "V_DS_IN": -0.4890,
        "V_DS_OUT": 0.5139,
        "V_US_OUT": -0.4613,
        "V_US_IN": 0.5076,
    },
}

FEA_COMPLIANCE: dict[str, dict[str, dict[str, float]]] = {
    "MS": _MS_COMPLIANCE,
    "SM": _MS_COMPLIANCE,  # same structure, same jack-frame-relative layout
    "LM": _LM_COMPLIANCE,
    "ML": _LM_COMPLIANCE,  # same structure, same jack-frame-relative layout
}
