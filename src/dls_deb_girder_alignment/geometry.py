"""
Girder alignment — geometry, Jacobian, pose fitting and move planning.

This module is the SINGLE AUTHORITATIVE implementation of the alignment maths.
The browser is a view only; it must not re-implement any of this.

Conventions (master CSYS, mm):
    +Z beam direction, +Y up, +X horizontal outboard.
Jack CSYS: rotated about Y per girder type, origin at the jack (bearing) centroid,
which lies at the girder's longitudinal centre.

Encoder sign conventions (rig-wide, identical on all four girders):
    vertical : -Y girder motion  = +count
    sway     : +X stage motion   = +count   (both ends)
    surge    : +Z stage motion   = +count U/S,  -count D/S   (D/S is INVERTED)

Sensing split (a consequence of the kinematic stack surge->sway->jacks->girder):
    vertical encoders read the GIRDER  -> sensitive to heave, pitch, roll
    surge/sway encoders read the STAGE -> translation only, immune to pitch/roll
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import numpy as np

from ._geom_data import GEOM, GEOMETRY_REVISION

# --------------------------------------------------------------------------
# DOF definitions.  Priority order is a WEIGHTING ranking, not a move order.
# --------------------------------------------------------------------------
AXES = [
    # key,     label,   unit,   default tolerance
    ("roll", "Roll", "mrad", 0.05),
    ("sway", "Sway", "mm", 0.10),
    ("heave", "Heave", "mm", 0.10),
    ("yaw", "Yaw", "mrad", 0.15),
    ("pitch", "Pitch", "mrad", 0.15),
    ("surge", "Surge", "mm", 0.20),
]
AXIS_KEYS = [a[0] for a in AXES]

VERT_IDS = ["V_US_IN", "V_US_OUT", "V_DS_IN", "V_DS_OUT"]
HORZ_IDS = ["SURGE_US", "SWAY_US", "SURGE_DS", "SWAY_DS"]
ENCODER_IDS = VERT_IDS + HORZ_IDS

# Parasitic (non-rigid) monitor thresholds, mm.
WARP_WARN, WARP_STOP = 0.003, 0.005
STRETCH_WARN, STRETCH_STOP = 0.003, 0.005

PROXIMITY_GATE_MM = 10.0  # survey point -> reference matching gate


# --------------------------------------------------------------------------
# Machine: resolved geometry for one girder type
# --------------------------------------------------------------------------
class Machine:
    """Resolved geometry for a single girder type."""

    def __init__(self, gtype: str):
        if gtype not in GEOM:
            raise KeyError(f"unknown girder type {gtype!r}; have {sorted(GEOM)}")
        self.type = gtype
        g = GEOM[gtype]
        self.raw = g
        self.label = g["label"]
        self.stl = g["stl"]
        self.z_range = g["z_range"]

        jf = g["jack_frame"]
        self.jx = np.array(jf["x_axis"], float)
        self.jy = np.array([0.0, 1.0, 0.0])
        self.jz = np.array(jf["z_axis"], float)
        self.p0 = np.array(jf["origin"], float)
        self.yaw_deg = jf["yaw_deg"]

        # IN/OUT and US/DS are DERIVED from position, never hand-labelled.
        self.bearings = self._label_points(g["bearings"], "J")
        self.venc = self._label_points(g["venc"], "V")

        self.henc = []
        for e in g["henc"]:
            d = dict(e)
            d["dir"] = self.jz if e["axis"] == "z" else self.jx
            self.henc.append(d)

        self.fids: dict[str, np.ndarray] = {
            k: np.array(v, float) for k, v in g["fids"].items()
        }

        self.J = self._build_jacobian()
        self.parasitic = self._parasitic_modes()

    # -- labelling -------------------------------------------------------
    def _tag(self, p: np.ndarray) -> str:
        d = np.asarray(p, float) - self.p0
        return (
            ("US" if float(d @ self.jz) < 0 else "DS")
            + "_"
            + ("IN" if float(d @ self.jx) < 0 else "OUT")
        )

    def _label_points(self, pts, prefix) -> list[dict]:
        m = {}
        for name, p in pts:
            pid = f"{prefix}_{self._tag(p)}"
            m[pid] = {"id": pid, "src_name": name, "p": np.array(p, float)}
        order = [f"{prefix}_{s}" for s in ("US_IN", "US_OUT", "DS_IN", "DS_OUT")]
        missing = [o for o in order if o not in m]
        if missing:
            raise ValueError(f"{self.type}: could not resolve {missing}")
        return [m[o] for o in order]

    # -- Jacobian --------------------------------------------------------
    def _build_jacobian(self) -> np.ndarray:
        """8x6 Jacobian: encoder_delta = J @ [tx,ty,tz, thx,thy,thz] about p0.

        The two encoder families need DIFFERENT levers, and this is easy to get
        wrong:

        * Vertical encoders contact the GIRDER underside, so they ride the rigid
          body and their own position is the correct lever. This is what produces
          the ~0.049 mm/mm cross-shift between jack pairs.

        * Surge/sway encoders read STAGE translation. A stage only ever
          translates — it never rotates — so the reading is the same wherever on
          the stage the encoder is mounted, and its own position is NOT a lever.
          The correct lever is the stage's location on the girder axis, i.e. the
          midpoint of that end's bearing pair.

        Consequence: yaw (which comes purely from differential sway) produces NO
        surge-encoder motion, because a lever lying along the girder axis rotated
        about Y moves perpendicular to that axis. Using the encoder position
        instead wrongly injects its transverse offset and invents a surge/yaw
        coupling that does not exist.
        """
        rows = []
        for e in self.venc:  # vertical: reads the girder
            r = e["p"] - self.p0
            rows.append([0.0, -1.0, 0.0, r[2], 0.0, -r[0]])
        for e in self.henc:  # horizontal: reads the stage
            r = self._stage_lever(e["id"])
            u = np.asarray(e["dir"], float)
            s = e["sign"]
            rows.append(
                [s * u[0], 0.0, s * u[2], 0.0, s * (u[0] * r[2] + u[2] * (-r[0])), 0.0]
            )
        return np.array(rows, float)

    def _stage_lever(self, enc_id: str) -> np.ndarray:
        """Lever for a stage-mounted encoder: its end's bearing-pair midpoint."""
        end = "US" if "_US" in enc_id else "DS"
        pts = [b["p"] for b in self.bearings if f"_{end}_" in b["id"]]
        return np.mean(pts, axis=0) - self.p0

    def _parasitic_modes(self) -> np.ndarray:
        """Left-null space of J: the two non-rigid encoder combinations."""
        rank = np.linalg.matrix_rank(self.J)
        _, _, vh = np.linalg.svd(self.J.T)
        return vh[rank:]  # (2, 8)

    # -- frame helpers ---------------------------------------------------
    def to_master(self, err: dict) -> np.ndarray:
        """Machine-frame pose (mm / mrad) -> master [t(3), theta(3) rad]."""
        t = (
            self.jx * err.get("sway", 0.0)
            + self.jy * err.get("heave", 0.0)
            + self.jz * err.get("surge", 0.0)
        )
        th = (
            self.jz * (err.get("roll", 0.0) / 1000.0)
            + self.jx * (err.get("pitch", 0.0) / 1000.0)
            + self.jy * (err.get("yaw", 0.0) / 1000.0)
        )
        return np.concatenate([t, th])

    def encoder_deltas(self, err: dict) -> dict[str, float]:
        """Encoder deltas that CORRECT the given pose error."""
        v = self.J @ (-self.to_master(err))
        return {eid: float(x) for eid, x in zip(ENCODER_IDS, v, strict=True)}

    def parasitic_of(self, reading_vec: Sequence[float]) -> tuple[float, float]:
        """(warp, stretch) content of an 8-vector of encoder values."""
        m = self.parasitic @ np.asarray(reading_vec, float)
        return float(m[0]), float(m[1])

    def rank(self) -> int:
        return int(np.linalg.matrix_rank(self.J))

    def summary(self) -> dict:
        return {
            "type": self.type,
            "yaw_deg": self.yaw_deg,
            # The browser draws plan/end/3D in the JACK frame. Without these
            # axes it silently falls back to the master frame with the origin at
            # zero, which puts the gizmo 5 m upstream and stops the end-view
            # points from overlapping.
            "jack_frame": {
                "x_axis": self.jx.tolist(),
                "y_axis": self.jy.tolist(),
                "z_axis": self.jz.tolist(),
                "origin": self.p0.tolist(),
                "yaw_deg": self.yaw_deg,
            },
            "origin": self.p0.tolist(),
            "rank": self.rank(),
            "cond": float(np.linalg.cond(self.J)),
            "n_fids": len(self.fids),
            "stl": self.stl,
            "z_range": self.z_range,
            "bearings": [{"id": b["id"], "p": b["p"].tolist()} for b in self.bearings],
            "venc": [{"id": e["id"], "p": e["p"].tolist()} for e in self.venc],
            "henc": [
                {"id": e["id"], "p": list(e["p"]), "kind": e["kind"], "sign": e["sign"]}
                for e in self.henc
            ],
            "fids": {k: v.tolist() for k, v in self.fids.items()},
            "revision": GEOMETRY_REVISION,
        }


_CACHE: dict[str, Machine] = {}


def machine(gtype: str) -> Machine:
    if gtype not in _CACHE:
        _CACHE[gtype] = Machine(gtype)
    return _CACHE[gtype]


# --------------------------------------------------------------------------
# Survey fitting
# --------------------------------------------------------------------------
@dataclass
class FitResult:
    err: dict[str, float]  # machine-frame pose error (mm / mrad)
    rms: float
    max_resid: float
    n_points: int
    matched: dict[str, list[float]]
    unmatched: list[list[float]]
    residuals: dict[str, float]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self):
        d = asdict(self)
        return d


def match_by_proximity(
    points: Sequence[Sequence[float]], m: Machine, gate: float = PROXIMITY_GATE_MM
):
    """Match measured XYZ to reference fiducials by nearest neighbour.

    Point *names* are not used: they repeat across girder types (MS/SM share
    APNT26-34, LM/ML share APNT47-55), so matching on position is what makes an
    uploaded survey safe to bind to a girder.
    """
    pts = [np.array(p, float) for p in points]
    used, matched = set(), {}
    for name, ref in m.fids.items():
        best, bd = None, 1e18
        for i, q in enumerate(pts):
            if i in used:
                continue
            d = float(np.linalg.norm(q - ref))
            if d < bd:
                bd, best = d, i
        if best is not None and bd <= gate:
            matched[name] = pts[best]
            used.add(best)
    unmatched = [pts[i].tolist() for i in range(len(pts)) if i not in used]
    return matched, unmatched


def fit_pose(measured: dict[str, Sequence[float]], m: Machine) -> FitResult:
    """Weighted small-angle least-squares rigid fit, nominal -> measured."""
    names = [n for n in m.fids if n in measured]
    if len(names) < 3:
        raise ValueError(f"need >=3 matched points, got {len(names)}")

    ata = np.zeros((6, 6))
    atb = np.zeros(6)
    for n in names:
        nom = m.fids[n]
        r = nom - m.p0
        d = np.asarray(measured[n], float) - nom
        rows = np.array(
            [
                [1, 0, 0, 0, r[2], -r[1]],
                [0, 1, 0, -r[2], 0, r[0]],
                [0, 0, 1, r[1], -r[0], 0],
            ],
            float,
        )
        ata += rows.T @ rows
        atb += rows.T @ d

    x = np.linalg.solve(ata, atb)
    t, th = x[:3], x[3:]

    resid, ss, mx = {}, 0.0, 0.0
    for n in names:
        nom = m.fids[n]
        r = nom - m.p0
        pred = t + np.cross(th, r)
        e = np.asarray(measured[n], float) - nom - pred
        d = float(np.linalg.norm(e))
        resid[n] = d
        ss += d * d
        mx = max(mx, d)

    err = {
        "sway": float(t @ m.jx),
        "heave": float(t @ m.jy),
        "surge": float(t @ m.jz),
        "roll": float(th @ m.jz) * 1000.0,
        "pitch": float(th @ m.jx) * 1000.0,
        "yaw": float(th @ m.jy) * 1000.0,
    }
    rms = math.sqrt(ss / len(names))

    warn = []
    if len(names) < len(m.fids):
        warn.append(f"only {len(names)} of {len(m.fids)} reference points matched")
    if rms > 0.10:
        warn.append(f"fit RMS {rms:.3f} mm is high — check for a moved fiducial")
    if mx > 3 * max(rms, 1e-6) and len(names) > 3:
        worst = max(resid, key=lambda k: resid[k])
        warn.append(
            f"{worst} residual {resid[worst]:.3f} mm dominates — possible bad point"
        )

    return FitResult(
        err=err,
        rms=rms,
        max_resid=mx,
        n_points=len(names),
        matched={k: list(map(float, v)) for k, v in measured.items() if k in names},
        unmatched=[],
        residuals=resid,
        warnings=warn,
    )


# --------------------------------------------------------------------------
# Move planning
# --------------------------------------------------------------------------
def pair_groups(m: Machine, pairing: str):
    if pairing == "side":
        return [
            {
                "key": "IN",
                "label": "Inboard",
                "bear": ["J_US_IN", "J_DS_IN"],
                "enc": ["V_US_IN", "V_DS_IN"],
            },
            {
                "key": "OUT",
                "label": "Outboard",
                "bear": ["J_US_OUT", "J_DS_OUT"],
                "enc": ["V_US_OUT", "V_DS_OUT"],
            },
        ]
    return [
        {
            "key": "US",
            "label": "U/S",
            "bear": ["J_US_IN", "J_US_OUT"],
            "enc": ["V_US_IN", "V_US_OUT"],
        },
        {
            "key": "DS",
            "label": "D/S",
            "bear": ["J_DS_IN", "J_DS_OUT"],
            "enc": ["V_DS_IN", "V_DS_OUT"],
        },
    ]


def _plane_fit(m: Machine, heights: dict[str, float]):
    """Least-squares plane a + b*x + c*z through the four bearing heights."""
    a = []
    y = []
    for b in m.bearings:
        p = b["p"]
        a.append([1.0, p[0], p[2]])
        y.append(heights[b["id"]])
    return np.linalg.lstsq(np.array(a), np.array(y), rcond=None)[0]


def _enc_from_heights(m: Machine, heights: dict[str, float]) -> dict[str, float]:
    a, b, c = _plane_fit(m, heights)
    out = {}
    for e in m.venc:
        p = e["p"]
        out[e["id"]] = float(-(a + b * p[0] + c * p[2]))  # -Y = +count
    return out


def pair_plan(m: Machine, sub: dict, pairing: str, first_idx: int) -> dict:
    """Vertical move split into two sequential pair moves.

    Each vertical encoder sits ~325 mm along-axis from its own bearing, so moving
    one pair re-tilts the girder about the other and shifts the un-moved pair's
    reading by ~0.049 mm per mm of far travel.  Consequently ONLY THE LAST PAIR
    MOVED LANDS ON FINAL TARGETS; the first pair aims at a compensated
    intermediate value.  This applies to ANY sequential pair move - flatten,
    heave, or the two combined.
    """
    v = -m.to_master(
        {
            "roll": sub.get("roll", 0.0),
            "pitch": sub.get("pitch", 0.0),
            "heave": sub.get("heave", 0.0),
        }
    )
    t, th = v[:3], v[3:]
    dh = {}
    for b in m.bearings:
        r = b["p"] - m.p0
        dh[b["id"]] = float(t[1] + th[2] * r[0] - th[0] * r[2])

    e_final = _enc_from_heights(m, dh)
    groups = pair_groups(m, pairing)
    first, second = groups[first_idx], groups[1 - first_idx]

    h_inter = {
        b["id"]: (dh[b["id"]] if b["id"] in first["bear"] else 0.0) for b in m.bearings
    }
    e_inter = _enc_from_heights(m, h_inter)

    # Full four-encoder state at each stage. The un-moved pair does NOT stay put:
    # the girder is rigid, so tilting it changes every vertical reading. Exposing
    # this lets the UI show EXPECTED values for the monitored encoders, so a
    # technician can tell "moved as expected" from "moved unexpectedly".
    return {
        "first": {
            "label": first["label"],
            "enc": first["enc"],
            "intermediate": [float(e_inter[i]) for i in first["enc"]],
            "final": [float(e_final[i]) for i in first["enc"]],
            "all": {k: float(v) for k, v in e_inter.items()},
        },
        "second": {
            "label": second["label"],
            "enc": second["enc"],
            "final": [float(e_final[i]) for i in second["enc"]],
            "all": {k: float(v) for k, v in e_final.items()},
        },
    }


def cross_shift_ratio(m: Machine) -> float:
    """mm of un-moved-pair encoder shift per mm of far-pair travel."""

    def along(p):
        return float((p - m.p0) @ m.jz)

    offs = [
        along(e["p"]) - along(b["p"]) for e, b in zip(m.venc, m.bearings, strict=True)
    ]
    span = max(along(b["p"]) for b in m.bearings) - min(
        along(b["p"]) for b in m.bearings
    )
    return abs(float(np.mean(np.abs(offs))) / span)


def compute_plan(
    m: Machine,
    err: dict,
    pairing: str = "end",
    first_idx: int = 0,
    combine_vertical: bool = False,
) -> dict:
    """Full move plan.

    Sequencing is set by COUPLING, not by tolerance priority:
      vertical first (roll creeps X/Y via the geometric lever), horizontal last
      (nearly one-way coupled, so it absorbs the creep without re-breaking it).
    """
    horiz_vec = -m.to_master(
        {
            "sway": err.get("sway", 0.0),
            "surge": err.get("surge", 0.0),
            "yaw": err.get("yaw", 0.0),
        }
    )
    horiz = m.J[4:] @ horiz_vec
    # state after the sway/yaw step but before surge — the surge encoders are
    # already displaced by the yaw at this point
    sway_vec = -m.to_master({"sway": err.get("sway", 0.0), "yaw": err.get("yaw", 0.0)})
    horiz_sway = m.J[4:] @ sway_vec

    plan = {
        "pairing": pairing,
        "first_idx": first_idx,
        "combine_vertical": combine_vertical,
        "cross_shift_ratio": cross_shift_ratio(m),
        "flatten": pair_plan(
            m,
            {"roll": err.get("roll", 0.0), "pitch": err.get("pitch", 0.0)},
            pairing,
            first_idx,
        ),
        "heave": pair_plan(m, {"heave": err.get("heave", 0.0)}, pairing, first_idx),
        "combined": pair_plan(
            m,
            {
                "roll": err.get("roll", 0.0),
                "pitch": err.get("pitch", 0.0),
                "heave": err.get("heave", 0.0),
            },
            pairing,
            first_idx,
        ),
        "horizontal": {hid: float(v) for hid, v in zip(HORZ_IDS, horiz, strict=True)},
        "horizontal_after_sway": {
            hid: float(v) for hid, v in zip(HORZ_IDS, horiz_sway, strict=True)
        },
    }
    plan["steps"] = build_steps(plan, combine_vertical)
    return plan


def build_steps(plan: dict, combine_vertical: bool) -> list[dict]:
    """Flatten the plan into the ordered list of technician move steps."""
    steps: list[dict] = []

    def vert_step(name, title, blk):
        for stage, label, tgt_key in (
            ("first", "move first", "intermediate"),
            ("second", "move last", "final"),
        ):
            b = blk[stage]
            exp = dict(b["all"])  # all four vertical encoders
            exp.update(dict.fromkeys(HORZ_IDS, 0.0))  # stages don't move here
            steps.append(
                {
                    "id": f"{name}_{stage}",
                    "group": name,
                    "title": f"{title} — {b['label']} pair ({label})",
                    "kind": "vertical",
                    "stage": stage,
                    "targets": dict(zip(b["enc"], b[tgt_key], strict=True)),
                    "final_targets": dict(zip(b["enc"], b["final"], strict=True)),
                    "expected": exp,
                    "note": (
                        "stop at the intermediate value — it settles to final "
                        "once the second pair moves"
                        if stage == "first"
                        else "final targets"
                    ),
                }
            )

    if combine_vertical:
        vert_step("combined", "Vertical (flatten + heave)", plan["combined"])
    else:
        vert_step("flatten", "Flatten (roll + pitch)", plan["flatten"])
        vert_step("heave", "Heave", plan["heave"])

    # Horizontal splits into two independent steps.
    #
    # Sway and surge are fully DECOUPLED: they act along perpendicular stage axes,
    # and because a stage only translates, yaw (which comes purely from differential
    # sway) produces no surge-encoder motion at all. Verified on the real geometry:
    # both cross-terms are zero to rounding. So both steps use final targets and the
    # order between them is free — sway is listed first only because it absorbs the
    # roll-induced lateral creep from the vertical stage.
    hz = plan["horizontal"]
    hz_sway = plan["horizontal_after_sway"]

    # The horizontal steps form their own datum group, so every encoder reads
    # zero when the group opens. The verticals are monitors here and should
    # simply stay there: a stage only translates, so a vertical excursion
    # during a horizontal move means the girder is riding a bearing.
    exp_sway = dict.fromkeys(VERT_IDS, 0.0)
    exp_sway.update(hz_sway)
    steps.append(
        {
            "id": "sway",
            "group": "horizontal",
            "title": "Horizontal 1 — sway and yaw",
            "kind": "horizontal",
            "stage": "sway",
            "targets": {e: hz[e] for e in ("SWAY_US", "SWAY_DS")},
            "final_targets": {e: hz[e] for e in ("SWAY_US", "SWAY_DS")},
            "expected": exp_sway,
            "note": "sets sway and yaw together (differential X) — absorbs the "
            "roll-induced lateral creep. Independent of surge, so the order "
            "between the two horizontal steps does not matter.",
        }
    )

    exp_surge = dict.fromkeys(VERT_IDS, 0.0)
    exp_surge.update(hz)
    steps.append(
        {
            "id": "surge",
            "group": "horizontal",
            "title": "Horizontal 2 — surge",
            "kind": "horizontal",
            "stage": "surge",
            "targets": {e: hz[e] for e in ("SURGE_US", "SURGE_DS")},
            "final_targets": {e: hz[e] for e in ("SURGE_US", "SURGE_DS")},
            "expected": exp_surge,
            "note": "both ends together — watch STRETCH; driving one end alone "
            "lets the girder ride up the far spherical bearing",
        }
    )
    for i, s in enumerate(steps):
        s["index"] = i
        s["monitors"] = [e for e in ENCODER_IDS if e not in s["targets"]]
    return steps


# --------------------------------------------------------------------------
# Assessment helpers
# --------------------------------------------------------------------------
def tolerances(overrides: dict | None = None) -> dict[str, float]:
    tol = {k: d for k, _, _, d in AXES}
    if overrides:
        tol.update({k: float(v) for k, v in overrides.items() if k in tol})
    return tol


def assess(err: dict, tol: dict[str, float]) -> dict:
    out, worst = {}, 0.0
    for k in AXIS_KEYS:
        v = float(err.get(k, 0.0))
        frac = abs(v) / tol[k] if tol[k] else 0.0
        worst = max(worst, frac)
        out[k] = {
            "value": v,
            "tol": tol[k],
            "frac": frac,
            "status": "ok" if frac <= 0.85 else ("warn" if frac <= 1.0 else "bad"),
        }
    return {"axes": out, "peak_frac": float(worst), "in_tolerance": bool(worst <= 1.0)}


def predicted_pose_change(m: Machine, achieved: dict[str, float]) -> dict:
    """Push achieved encoder deltas through J to predict the pose change.

    Comparing this against the next survey validates the geometric model on real
    hardware at no extra cost, and feeds the residual-correction hook.
    """
    d = np.array([achieved.get(e, 0.0) for e in ENCODER_IDS], float)
    sol, *_ = np.linalg.lstsq(m.J, d, rcond=None)
    t, th = sol[:3], sol[3:]
    return {
        "sway": float(t @ m.jx),
        "heave": float(t @ m.jy),
        "surge": float(t @ m.jz),
        "roll": float(th @ m.jz) * 1000.0,
        "pitch": float(th @ m.jx) * 1000.0,
        "yaw": float(th @ m.jy) * 1000.0,
    }
