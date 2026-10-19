#!/usr/bin/env python3
"""Suggest calibration corrections from accumulated alignment sessions.

WHAT THIS DOES. Every completed move the tool ever commands carries two
independent estimates of the same thing: a PREDICTED pose change, pushed
through the geometric model (the rigid Jacobian, plus - for a girder type
with FEA compliance data - the warp amplitude) from the achieved encoder
deltas; and an ACTUAL pose change, read off the next real survey. The tool
already stores both, per round of moves, in each session's
``moves[i]["model_check"]`` (summed correctly across every step since the
previous survey - not just the last one). This script pools that across
every session you give it and looks for a SYSTEMATIC relationship between
predicted and actual: if real hardware consistently moves ~10% more or less
than the model predicts for a given axis, that shows up as a regression
slope away from 1.0, not just noise around 0.

    - For the 6 RIGID axes (roll, sway, heave, yaw, pitch, surge), the
      model is exact kinematics (lever arms from the bearing/encoder
      geometry) - a slope that drifts from 1.0 across many sessions points
      at a GEOMETRY error (e.g. a bearing pivot position), not something to
      "retune" the way a gain is.
    - For WARP, the model is an FEA-derived compliance gain
      (``_fea_data.FEA_COMPLIANCE``) multiplied into the same prediction -
      a slope away from 1.0 here is exactly the signal that the stored gain
      for that girder type should be scaled by that slope.

WHAT THIS DOESN'T DO. This finds a single uniform SCALE correction per
(girder type, axis) - it cannot tell you that one individual vencoder's
gain is wrong relative to the others (a SHAPE error), only that the whole
set is collectively too strong or too weak. It also only ever sees warp
residuals for a girder type that already has FEA compliance data -
"predicted" has no "warp" key at all for one that doesn't (see
``geometry.predicted_pose_change``), and those rounds are silently
excluded rather than counted as a false "0 predicted" result.

USAGE. Export the session store, then point this at the export(s):

    dls-deb-girder-alignment sessions export --out export.json
    scripts/calibrate.py export.json

Accepts more than one file (e.g. several bays, or several dated snapshots
from scripts/mirror_reports.py --sessions) - sessions are pooled by id, so
the same session appearing in more than one file is only counted once.
Understands both the raw ``{id: session}`` shape the CLI command writes and
the ``{"sessions": {id: session}}`` shape ``/api/sessions/export`` (and
mirror_reports.py) return.

Standard library only, so it needs no virtualenv on the machine that runs
it - the same reasoning as scripts/mirror_reports.py.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

#: The 6 correctable rigid DOF, plus the one parasitic mode this tool has an
#: FEA-derived compliance model for. Anything else in a move's model_check
#: (there isn't anything else, but defensively) is ignored.
AXES = ["roll", "sway", "heave", "yaw", "pitch", "surge", "warp"]

#: Below this many samples, a regression slope is noise dressed as a number.
DEFAULT_MIN_SAMPLES = 5

#: Below this |r|, "predicted" barely explains "actual" at all - a large N
#: does NOT rescue a weak correlation here. Found empirically running this
#: against a real accumulated store: 19 samples with r=0.11 produced a
#: slope of 40x that looked "confident" on sample count alone and was
#: obvious nonsense once the correlation was checked too.
MIN_CORRELATION = 0.3


def load_sessions(paths: list[Path]) -> dict[str, dict]:
    """Pool sessions from one or more export files, unwrapping either shape."""
    out: dict[str, dict] = {}
    for p in paths:
        raw = json.loads(p.read_text())
        has_wrapper = isinstance(raw, dict) and "sessions" in raw
        sessions = raw["sessions"] if has_wrapper else raw
        if not isinstance(sessions, dict):
            raise ValueError(f"{p}: not a recognised sessions export")
        out.update(sessions)
    return out


def collect_samples(
    sessions: dict[str, dict], gtype_filter: str | None = None
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """-> {(gtype, axis): [(predicted, actual), ...]} across every session.

    Only axes that were GENUINELY predicted are collected - for "warp" that
    means the round's predicted dict actually has a "warp" key (the girder
    type had FEA data at the time), not the fallback where residual is just
    a copy of actual. Including those would make it look like the model
    agrees with itself, which proves nothing.
    """
    out: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for sess in sessions.values():
        gtype = sess.get("gtype")
        if not gtype or (gtype_filter and gtype != gtype_filter):
            continue
        for mv in sess.get("moves") or []:
            mc = mv.get("model_check")
            if not mc:
                continue
            predicted, actual = mc.get("predicted") or {}, mc.get("actual") or {}
            for axis in AXES:
                if axis not in predicted:
                    continue  # not genuinely predicted this round - see docstring
                a = actual.get(axis)
                if a is None:
                    continue
                out[(gtype, axis)].append((float(predicted[axis]), float(a)))
    return out


def regress_through_origin(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares slope k (actual ~= k * predicted) and Pearson r.

    Through the origin, not a general line: a perfect model predicts actual=0
    exactly when predicted=0 (no move commanded, nothing should have
    happened), so there is no meaningful intercept to fit here - fitting one
    anyway would just launder a wrongly-matched survey pair into a fake
    "offset" term.
    """
    preds = [p for p, _ in pairs]
    acts = [a for _, a in pairs]

    # Below ~0.1 um/urad of spread in what was predicted, every "predicted"
    # value is indistinguishable from float noise around a real zero (no
    # move commanded, nothing should have happened) - sum_pp is then tiny
    # but not exactly zero, so dividing by it gives a slope that LOOKS like
    # a number (e.g. "-0.000") but is actually dividing noise by noise.
    # Refuse to report one rather than print false precision.
    if statistics.pstdev(preds) < 1e-4:
        return float("nan"), float("nan")

    sum_pp = sum(p * p for p in preds)
    sum_pa = sum(p * a for p, a in zip(preds, acts, strict=True))
    slope = sum_pa / sum_pp

    r = (
        statistics.correlation(preds, acts)
        if len(pairs) >= 2 and statistics.pstdev(acts) > 1e-9
        else float("nan")
    )
    return slope, r


def summarise(
    samples: dict[tuple[str, str], list[tuple[float, float]]], min_samples: int
) -> list[dict]:
    rows = []
    for (gtype, axis), pairs in sorted(samples.items()):
        residuals = [a - p for p, a in pairs]
        slope, r = regress_through_origin(pairs)
        slope_known = slope == slope  # False for NaN
        enough_samples = len(pairs) >= min_samples
        enough_correlation = r == r and abs(r) >= MIN_CORRELATION  # r==r: not NaN
        rows.append(
            {
                "gtype": gtype,
                "axis": axis,
                "n": len(pairs),
                "mean_residual": statistics.fmean(residuals),
                "std_residual": statistics.pstdev(residuals) if len(pairs) > 1 else 0.0,
                "slope": slope,
                "r": r,
                "confident": enough_samples and enough_correlation,
                # Only worth calling out separately from "not enough spread
                # to fit" (already said in the suggestion column) when a
                # slope WAS fit but its correlation is too weak to trust.
                "weak_correlation": (
                    enough_samples and slope_known and not enough_correlation
                ),
            }
        )
    return rows


def print_report(rows: list[dict], min_samples: int) -> None:
    if not rows:
        print("No model_check data found in the given export(s) - nothing to report.")
        print(
            "model_check is only recorded when a round of moves is followed by "
            "another survey, so at least one full solve-move-resurvey cycle "
            "per girder is needed before this has anything to say."
        )
        return

    print(
        f"{'type':<5}{'axis':<8}{'n':>5}  {'mean resid':>11}  {'std':>8}  "
        f"{'slope':>7}  {'r':>6}   suggestion"
    )
    print("-" * 92)
    for row in rows:
        if row["confident"]:
            flag = ""
        elif row["weak_correlation"]:
            flag = "  (weak correlation - slope is not trustworthy despite the N)"
        elif row["n"] < min_samples:
            flag = "  (few samples - treat as indicative only)"
        else:
            flag = "  (predicted values too close to zero to fit a slope)"
        unit = "mrad" if row["axis"] in ("roll", "yaw", "pitch") else "mm"
        slope_known = row["slope"] == row["slope"]  # False for NaN
        if row["axis"] == "warp":
            suggestion = (
                f"scale stored FEA venc gains by {row['slope']:.3f}"
                if slope_known
                else "not enough spread to fit"
            )
        elif not slope_known:
            suggestion = "not enough spread to fit"
        else:
            suggestion = (
                "rigid geometry looks consistent"
                if abs(row["slope"] - 1.0) < 0.02
                else f"check geometry - kinematics off by {row['slope']:.3f}x"
            )
        print(
            f"{row['gtype']:<5}{row['axis']:<8}{row['n']:>5}  "
            f"{row['mean_residual']:>+9.4f}{unit:>2}  {row['std_residual']:>8.4f}  "
            f"{row['slope']:>7.3f}  {row['r']:>6.3f}   {suggestion}{flag}"
        )
    print()
    print(
        "slope = actual/predicted (1.0 = model matches hardware exactly). "
        "r = Pearson correlation (closer to +-1 = more trustworthy slope - a "
        f"large N with |r| < {MIN_CORRELATION} is flagged, not treated as "
        "confident, since the slope it implies is mostly explained by noise). "
        "mean/std residual are in the axis's own unit (mrad for roll/yaw/pitch, "
        "mm for the rest) and show up even when slope can't be fit."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "exports", nargs="+", type=Path, help="one or more session export JSON files"
    )
    parser.add_argument(
        "--type", default=None, help="only this girder type (e.g. MS) - default: all"
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"samples needed before a slope is shown as reliable (default: "
        f"{DEFAULT_MIN_SAMPLES})",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the summary as JSON instead"
    )
    args = parser.parse_args(argv)

    try:
        sessions = load_sessions(args.exports)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1

    samples = collect_samples(sessions, args.type)
    rows = summarise(samples, args.min_samples)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{len(sessions)} session(s) loaded.\n")
        print_report(rows, args.min_samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
