"""
Alignment session — state machine, persistence, gating and audit trail.

One girder per bay, so one active session at a time.

Step-back is deliberately NOT an undo. The UI can rewind; the steel cannot.
Re-opening a step recomputes its targets from the CURRENT encoder readings.
Replaying targets computed for a pose that no longer exists would be actively
dangerous, so no code path does it.

Every state change is appended to an event log and written to SQLite, so a
browser crash or a pod restart loses nothing and the report can be reconstructed.

The PLAN DATUM lives here too. Step targets are encoder *deltas* from the pose
that was surveyed, so a reading only means something measured from the value the
encoder had when its move group opened. That datum is captured automatically on
entering a group and is part of the persisted session, so a restart mid-move
resumes against the same reference rather than silently changing it. It is
distinct from the IOC-side encoder zero, which is a commissioning action.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from . import geometry as G

SOFTWARE_VERSION = __version__
DEFAULT_GATE_MM = 0.010  # per-encoder "on target" gate

#: Axes each move-group's own plan actually solves for - see
#: Session.estimate_live_pose for why this matters. "horizontal" has no
#: intermediate/final split (sway/surge go straight to final targets, see
#: geometry.build_steps), so it has no equivalent asymmetric-mid-group risk,
#: but is listed anyway for a consistent, exhaustive mapping.
#:
#: "warp" is included for every VERTICAL group (flatten/heave/combined), not
#: just heave/combined (the only ones whose own plan deliberately targets a
#: nonzero warp correction) - any group driving vertical-encoder pairs
#: sequentially can show transient, asymmetric warp content mid-group purely
#: from one pair being done and the other not (the same cross-shift effect
#: that affects roll/pitch/heave themselves), so the live full-body warp
#: estimate can't be trusted there either. horizontal never touches the
#: vertical encoders at all, so warp isn't excluded for it - whatever warp
#: content a vertical group already finished is real signal there, not
#: mid-group noise. predicted_pose_change only recovers a "warp" key at all
#: for a girder type with FEA vencoder compliance data (see its docstring) -
#: for one without, this set membership is simply never consulted for warp.
_GROUP_AXES = {
    "flatten": {"roll", "pitch", "warp"},
    "heave": {"heave", "warp"},
    "combined": {"roll", "pitch", "heave", "warp"},
    "horizontal": {"sway", "surge", "yaw"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SessionStore:
    """SQLite-backed store. Sessions survive restarts and can be resumed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                serial TEXT, gtype TEXT, operator TEXT,
                created TEXT, updated TEXT, status TEXT,
                blob TEXT
            )""")
        self._db.commit()

    def close(self) -> None:
        """Release the SQLite connection.

        The service holds one store for the life of the process, so this is
        rarely called in anger. It matters anywhere a store is short-lived -
        the tests, and `sessions` on the command line - because from Python
        3.13 an unclosed connection raises ResourceWarning when it is
        collected, and the suite runs with warnings as errors.
        """
        self._db.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save(self, s: Session):
        d = s.to_dict()
        self._db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (
                s.id,
                s.serial,
                s.gtype,
                s.operator,
                s.created,
                _now(),
                s.status,
                json.dumps(d),
            ),
        )
        self._db.commit()

    def export(self, limit: int = 10_000) -> dict[str, dict]:
        """Every session, whole, keyed by id.

        The report written beside each PDF already contains its own session, so
        this is only the safety net for sessions that never produced one -
        abandoned partway, or open when the volume is lost. Reading the rows
        through SQLite gives a consistent snapshot, which copying the database
        file out from under a running service does not.
        """
        out = {}
        for row in self.list(limit=limit):
            d = self.load(row["id"])
            if d is not None:
                out[row["id"]] = d
        return out

    def load(self, sid: str) -> dict | None:
        r = self._db.execute("SELECT blob FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(r[0]) if r else None

    def list(self, limit: int = 50) -> builtins.list[dict]:
        rows = self._db.execute(
            "SELECT id,serial,gtype,operator,created,updated,status "
            "FROM sessions ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = ["id", "serial", "gtype", "operator", "created", "updated", "status"]
        return [dict(zip(cols, r, strict=True)) for r in rows]


class Session:
    """One girder alignment, start to as-left."""

    def __init__(
        self,
        serial: str,
        gtype: str,
        operator: str,
        pairing: str = "end",
        first_idx: int = 0,
        combine_vertical: bool = True,
        gate_mm: float = DEFAULT_GATE_MM,
        tol_overrides: dict | None = None,
        heave_bias_mm: float | None = None,
        bay: str = "",
        sid: str | None = None,
    ):
        self.id = sid or uuid.uuid4().hex[:12]
        self.serial = serial
        self.bay = str(bay or "")
        self.gtype = gtype
        self.operator = operator
        self.created = _now()
        self.status = "active"  # active | complete | stopped

        self.pairing = pairing
        self.first_idx = first_idx
        self.combine_vertical = combine_vertical
        self.gate_mm = float(gate_mm)
        self.tol = G.tolerances(tol_overrides)

        self.machine = G.machine(gtype)
        self.geometry_revision = G.GEOMETRY_REVISION
        self.software_version = SOFTWARE_VERSION

        # Bare-girder heave pre-set (see geometry.compute_plan and
        # _fea_data.SAG_BIAS_MM). None means "use this girder type's usual
        # recommendation"; resolved to a concrete number now so the record
        # shows exactly what was used, not "default".
        self.heave_bias_mm = (
            self.machine.sag_bias_mm if heave_bias_mm is None else float(heave_bias_mm)
        )

        self.iteration = 0
        self.phase = (
            "await_survey"  # await_survey | solved | moving | verify | complete
        )
        self.step_index = 0

        self.initial_err: dict | None = None
        self.current_err: dict | None = None
        self.plan: dict | None = None
        self.surveys: list[dict] = []
        self.moves: list[dict] = []
        self.overrides: list[dict] = []
        self.events: list[dict] = []
        self.reports: list[dict] = []
        self.temperature: float | None = None
        self.notes: str = ""

        # Plan datum: encoder readings when the current move group opened, and
        # the group it belongs to. Targets are measured from this.
        self.datum: dict[str, float] = {}
        self.datum_group: str = ""

        self.log(
            "session_start",
            serial=serial,
            gtype=gtype,
            operator=operator,
            bay=self.bay,
            geometry_revision=self.geometry_revision,
            software_version=self.software_version,
        )

    # -- audit -----------------------------------------------------------
    def log(self, kind: str, **kw):
        self.events.append({"t": _now(), "kind": kind, **kw})

    # -- survey ----------------------------------------------------------
    def add_survey(
        self,
        points: list[list[float]],
        source: str = "upload",
        temperature: float | None = None,
        temperature_detail: dict | None = None,
        filename: str = "",
        comment: str = "",
    ) -> dict:
        m = self.machine
        matched, unmatched = G.match_by_proximity(points, m)
        if len(matched) < 3:
            raise ValueError(
                f"only {len(matched)} points matched within "
                f"{G.PROXIMITY_GATE_MM} mm of the {self.gtype} reference points. "
                "Check the file is in the master frame and is the right girder."
            )
        fit = G.fit_pose(matched, m)
        fit.unmatched = unmatched
        if unmatched:
            fit.warnings.append(f"{len(unmatched)} uploaded point(s) matched nothing")

        self.iteration += 1
        rec = {
            "iteration": self.iteration,
            "t": _now(),
            "source": source,
            "filename": filename,
            "comment": comment.strip(),
            "n_uploaded": len(points),
            "temperature": temperature,
            "temperature_detail": temperature_detail,
            "fit": {
                "rms": fit.rms,
                "max": fit.max_resid,
                "n": fit.n_points,
                "residuals": fit.residuals,
                "warnings": fit.warnings,
            },
            "err": fit.err,
            "points": [list(map(float, p)) for p in points],
            "matched": {k: list(map(float, v)) for k, v in matched.items()},
            "assessment": G.assess(
                fit.err, self.tol, target={"heave": self.heave_bias_mm}
            ),
        }
        self.surveys.append(rec)
        if temperature is not None:
            self.temperature = temperature
        self.current_err = dict(fit.err)
        if self.initial_err is None:
            self.initial_err = dict(fit.err)

        # Validate the achieved moves against the model, for free. Every move
        # made since the PREVIOUS survey contributed to the change this
        # survey now measures - not just the last one - so the prediction
        # combines all of them. Comparing the whole actual change against
        # only the final step's own (typically small, single-axis) predicted
        # change was a real bug: a heave correction made two steps ago would
        # show up entirely as "residual" once the last step was, say, a
        # surge move that predicts ~0 heave change of its own.
        #
        # But a flat SUM over every move is wrong too: moves within one
        # group share a single datum (see capture_datum), so each move's
        # own `predicted_pose_change` is the CUMULATIVE change implied by
        # the encoders relative to that shared datum, not an incremental
        # delta since the previous move in the same group - summing both
        # of a group's steps double-counts almost the whole group's
        # contribution. Only the LAST move in each group reflects that
        # group's true total (same reasoning as estimate_live_pose's
        # last-move-per-group handling for step-back).
        this_iter_moves = [
            mv for mv in self.moves if mv["iteration"] == self.iteration - 1
        ]
        if this_iter_moves:
            prev = self.surveys[-2]["err"] if len(self.surveys) > 1 else None
            if prev:
                last_by_group: dict[str, dict] = {}
                for mv in this_iter_moves:
                    last_by_group[mv.get("datum_group") or ""] = mv
                predicted: dict[str, float] = {}
                for mv in last_by_group.values():
                    for k, v in (mv.get("predicted_pose_change") or {}).items():
                        predicted[k] = predicted.get(k, 0.0) + v
                actual = {k: self.current_err[k] - prev[k] for k in self.current_err}
                this_iter_moves[-1]["model_check"] = {
                    "steps": [mv["step"] for mv in this_iter_moves],
                    "predicted": predicted,
                    "actual": actual,
                    "residual": {
                        k: actual[k] - predicted.get(k, 0.0) for k in actual
                    },
                }

        # "solved" is a real waiting state: the pose error is shown, but the
        # plan is not built until the operator explicitly accepts it (see
        # accept_plan) - a technician should see and confirm what's about to
        # be commanded before targets appear, not have it happen the instant
        # a fit comes back. This is true even when the survey is already IN
        # tolerance: the session is marked complete either way (below), but
        # the operator can still choose to accept the survey and generate a
        # further round of (small) correction steps rather than stop here.
        self.phase = "solved"
        self.log(
            "survey",
            iteration=self.iteration,
            rms=fit.rms,
            n=fit.n_points,
            warnings=fit.warnings,
            err=fit.err,
        )
        if rec["assessment"]["in_tolerance"]:
            self.status = "complete"
            self.log("converged", err=fit.err)
        elif self.status == "complete":
            # A prior survey converged and marked us complete, but this new
            # one (e.g. the operator re-checking, or picking a different
            # file) is out of tolerance again - we're back in play.
            self.status = "active"
        return rec

    # -- planning --------------------------------------------------------
    def accept_plan(self) -> dict:
        """Operator confirms the survey; build and enter the move plan.

        The only caller-facing difference from calling :meth:`solve` directly
        is the guard: this is the "solved" -> "moving" transition a technician
        triggers on purpose, so refuse it from any other phase rather than
        silently rebuilding whatever plan happens to exist (that's what
        :meth:`set_options`/:meth:`step_back` are for, mid-move).
        """
        if self.phase != "solved":
            raise RuntimeError(f"nothing to accept in phase {self.phase!r}")
        return self.solve()

    def solve(self) -> dict:
        if self.current_err is None:
            raise RuntimeError("no survey yet")
        self.plan = G.compute_plan(
            self.machine,
            self.current_err,
            self.pairing,
            self.first_idx,
            self.combine_vertical,
            heave_bias_mm=self.heave_bias_mm,
        )
        self.step_index = 0
        self.clear_datum()  # targets are new; the old reference is meaningless
        self.phase = "moving"
        if self.status == "complete":
            # Building a plan means real moves are about to happen again -
            # including the "already in tolerance, refine further anyway"
            # path add_survey now allows. A session mid-move is active,
            # whatever an earlier survey's tolerance verdict said.
            self.status = "active"
        self.log(
            "solve",
            n_steps=len(self.plan["steps"]),
            combine_vertical=self.combine_vertical,
            pairing=self.pairing,
        )
        return self.plan

    def set_options(self, **kw):
        """Change pairing / first pair / combine mode, then re-solve."""
        keys = ("pairing", "first_idx", "combine_vertical", "gate_mm", "heave_bias_mm")
        for k in keys:
            if k in kw and kw[k] is not None:
                setattr(self, k, kw[k])
        if "tol_overrides" in kw and kw["tol_overrides"]:
            self.tol = G.tolerances(kw["tol_overrides"])
        self.log(
            "options",
            pairing=self.pairing,
            first_idx=self.first_idx,
            combine_vertical=self.combine_vertical,
            gate_mm=self.gate_mm,
            heave_bias_mm=self.heave_bias_mm,
        )
        if self.current_err is not None:
            self.solve()

    # -- steps -----------------------------------------------------------
    @property
    def steps(self) -> list[dict]:
        return self.plan["steps"] if self.plan else []

    def current_step(self) -> dict | None:
        if not self.plan or self.step_index >= len(self.steps):
            return None
        return self.steps[self.step_index]

    def needs_datum(self, step: dict | None = None) -> bool:
        """True when the step about to be shown belongs to a new move group."""
        step = step or self.current_step()
        return step is not None and self.datum_group != step["group"]

    def capture_datum(self, snapshot: dict[str, float], group: str) -> dict:
        """Record where the encoders were when this move group opened.

        Each group's targets are deltas from the state at the start of that
        group - flatten targets assume the girder as surveyed, heave targets
        assume flatten has already been applied, and so on - so the datum has to
        be re-taken at every group boundary or the gate compares against the
        wrong reference.
        """
        self.datum = {k: float(v) for k, v in snapshot.items()}
        self.datum_group = group
        self.log("datum", group=group, values=dict(self.datum))
        return {"group": group, "datum": dict(self.datum)}

    def clear_datum(self) -> None:
        self.datum = {}
        self.datum_group = ""

    def check_step(self, readings: dict[str, dict], step: dict | None = None) -> dict:
        """Evaluate the tolerance gate for a step.

        The gate tests the parasitic monitors as well as the target encoders:
        being on target with WARP out of limit is not a step to wave through.
        """
        step = step or self.current_step()
        if step is None:
            return {"ok": True, "reason": "no active step"}

        # 'worst' must stay JSON-safe. float('inf') serialises as bare Infinity,
        # which JavaScript's JSON.parse rejects — that killed the browser poll
        # loop whenever an encoder was disconnected.
        per, worst, missing = {}, 0.0, False
        for eid, tgt in step["targets"].items():
            rel = (readings.get(eid) or {}).get("relative")
            if rel is None:
                per[eid] = {
                    "target": float(tgt),
                    "actual": None,
                    "error": None,
                    "ok": False,
                    "reason": "no reading",
                }
                missing = True
                continue
            e = rel - tgt
            per[eid] = {
                "target": float(tgt),
                "actual": float(rel),
                "error": float(e),
                "ok": bool(abs(e) <= self.gate_mm),
            }
            worst = max(worst, abs(float(e)))

        # Compared against the STEP'S OWN expected parasitic content, not zero:
        # a warp-corrected heave move deliberately targets nonzero WARP (see
        # compute_plan), so the gate must catch UNEXPECTED parasitic content
        # (racking, binding) rather than flag every warp correction as a fault.
        vec = [(readings.get(e) or {}).get("relative") or 0.0 for e in G.ENCODER_IDS]
        warp_raw, stretch_raw = self.machine.parasitic_of(vec)
        exp_vec = [step["expected"].get(e, 0.0) for e in G.ENCODER_IDS]
        warp_exp, stretch_exp = self.machine.parasitic_of(exp_vec)
        warp, stretch = warp_raw - warp_exp, stretch_raw - stretch_exp
        par_ok = bool(abs(warp) <= G.WARP_STOP and abs(stretch) <= G.STRETCH_STOP)

        # A reading's age is deliberately not part of this. The encoders are
        # static for most of a measurement and their records only process on
        # change, so an old timestamp is normal; gating on it would refuse to
        # pass a step exactly when the girder has stopped moving. A PV that is
        # not connected has no value, which `missing` already catches.
        ok = bool(all(v["ok"] for v in per.values()) and par_ok and not missing)
        reasons = []
        if missing:
            gone = [e for e, v in per.items() if v.get("actual") is None]
            reasons.append(
                "no reading from " + ", ".join(gone) + " — check the EPICS connection"
            )
        if not all(v["ok"] for v in per.values()):
            reasons.append(
                f"encoder off target by up to {worst:.4f} mm "
                f"(gate {self.gate_mm:.4f} mm)"
            )
        if not par_ok:
            reasons.append(
                f"parasitic limit exceeded: WARP {warp:+.4f}, STRETCH {stretch:+.4f} mm"
            )

        return {
            "ok": ok,
            "per_encoder": per,
            "worst": (None if missing else float(worst)),
            "missing": missing,
            "warp": float(warp),
            "stretch": float(stretch),
            "parasitic_ok": par_ok,
            "gate_mm": self.gate_mm,
            "reasons": reasons,
        }

    def complete_step(
        self,
        readings: dict[str, dict],
        override: bool = False,
        reason: str = "",
        operator: str = "",
        comment: str = "",
    ) -> dict:
        step = self.current_step()
        if step is None:
            raise RuntimeError("no active step")
        chk = self.check_step(readings, step)
        if not chk["ok"] and not override:
            return {"accepted": False, "check": chk}

        achieved = {e: (readings.get(e) or {}).get("relative") for e in G.ENCODER_IDS}
        rec = {
            "t": _now(),
            "iteration": self.iteration,
            "step": step["id"],
            "title": step["title"],
            "kind": step["kind"],
            "targets": dict(step["targets"]),
            "achieved": {e: achieved.get(e) for e in step["targets"]},
            "all_encoders": achieved,
            "errors": {e: chk["per_encoder"][e]["error"] for e in chk["per_encoder"]},
            "warp": chk["warp"],
            "stretch": chk["stretch"],
            "gate_mm": self.gate_mm,
            "datum": dict(self.datum),
            "datum_group": self.datum_group,
            "override": bool(override and not chk["ok"]),
            "override_reason": reason if (override and not chk["ok"]) else "",
            "override_by": operator or self.operator,
            "comment": comment.strip(),
            "reopened": False,
            "predicted_pose_change": G.predicted_pose_change(
                self.machine, {k: v for k, v in achieved.items() if v is not None}
            ),
        }
        self.moves.append(rec)
        if rec["override"]:
            self.overrides.append(
                {
                    "t": rec["t"],
                    "step": step["id"],
                    "by": rec["override_by"],
                    "reason": reason,
                    "worst_error": chk["worst"],
                    "gate_mm": self.gate_mm,
                    "warp": chk["warp"],
                    "stretch": chk["stretch"],
                    "reasons": chk["reasons"],
                }
            )
            self.log(
                "gate_override",
                step=step["id"],
                by=rec["override_by"],
                reason=reason,
                worst=chk["worst"],
            )
        self.log("step_complete", step=step["id"], override=rec["override"])

        self.step_index += 1
        if self.step_index >= len(self.steps):
            self.phase = "verify"
        return {"accepted": True, "check": chk, "move": rec}

    def estimate_live_pose(self, readings: dict[str, dict]) -> dict:
        """Best current estimate of the pose error, for :meth:`step_back`.

        ``current_err`` only ever changes on a new survey, and a live reading's
        ``relative`` value is only meaningful against the CURRENT datum — so
        neither one alone sees a correction already completed in an EARLIER,
        different group (flatten vs heave vs horizontal): that group's datum
        has since been superseded, so it reads as zero in a live poll even
        though the girder genuinely moved. Add both together: completed moves
        in a different datum group since the last survey (via each move's own
        stored ``predicted_pose_change``, since their datum is gone), plus
        whatever the live reading shows for progress in the CURRENT group.

        Filtered by ``iteration``, not by comparing timestamps: ``t`` has only
        second resolution, so a move made in the same second as the survey
        that triggered it (routine in an automated demo, and not impossible by
        hand either) would otherwise be wrongly treated as pre-dating it.

        Only the LAST move in each other group is used, not a sum of every
        move there: every move within one group is measured from the SAME
        shared datum (see the module docstring), so a later move's own
        ``achieved`` already includes an earlier one's progress — summing
        both would double-count it.

        The live reading's OWN GROUP's axes (``_GROUP_AXES``) are excluded
        from what it contributes, and left exactly as ``current_err`` already
        has them. This looks backwards — surely a live reading tells you
        MORE about the group you're actually standing in? — but every group's
        own plan is a UNIFORM, no-differential move (heave is a pure Y shift,
        flatten a pure tilt, never both at once): it cannot express "the first
        pair is done, the second still needs to move X more". Reopening a
        group after only one of its two pair-moves has completed leaves the
        girder in a genuinely asymmetric state, and no single scalar value fed
        into that group's uniform formula reproduces it — one pair or the
        other always ends up wrong. The already-completed pair's target was
        proven correct when it passed its own gate; leaving that axis alone
        (falling back to whatever it was solved from originally) reproduces
        that same, still-correct plan for the group, rather than corrupting
        it with a number a uniform formula can't actually use correctly.
        Cross-group information has no such problem — a DIFFERENT, fully
        finished group's own recorded correction is exact, which is what
        ``accumulated`` above already uses.
        """
        last_by_group: dict[str, dict] = {}
        for mv in self.moves:
            if mv.get("iteration") != self.iteration:
                continue  # from a superseded iteration, before the last survey
            grp = mv.get("datum_group") or ""
            if grp == self.datum_group:
                continue  # same datum as now - already in the live reading
            last_by_group[grp] = mv.get("predicted_pose_change") or {}

        accumulated: dict[str, float] = {}
        for pose_change in last_by_group.values():
            for k, v in pose_change.items():
                accumulated[k] = accumulated.get(k, 0.0) + v

        vec = {e: (readings.get(e) or {}).get("relative") for e in G.ENCODER_IDS}
        vec = {k: v for k, v in vec.items() if v is not None}
        change = G.predicted_pose_change(self.machine, vec)
        own_axes = _GROUP_AXES.get(self.datum_group) or set()
        change = {k: v for k, v in change.items() if k not in own_axes}

        base = self.current_err or {}
        keys = set(base) | set(accumulated) | set(change)
        return {
            k: base.get(k, 0.0) + accumulated.get(k, 0.0) + change.get(k, 0.0)
            for k in keys
        }

    def _flag_reopened(self, step_id: str) -> None:
        """Mark the most recent completed move for this step as reopened.

        Called right before a step is stepped back into - it was already
        confirmed complete once, and is about to be redone. The report
        highlights any move flagged this way, since it's not the entry that
        should be read as the step's final outcome (the fresh move recorded
        once it's redone is).
        """
        for mv in reversed(self.moves):
            if mv["iteration"] == self.iteration and mv["step"] == step_id:
                mv["reopened"] = True
                return

    def step_back(self, live_err: dict | None = None) -> dict:
        """Re-open the previous step.

        Targets are recomputed from the CURRENT pose, never replayed. If a fresh
        pose estimate is not available the caller should re-survey instead.
        """
        if self.step_index == 0:
            return {"ok": False, "reason": "already at the first step"}
        target_index = self.step_index - 1
        self._flag_reopened(self.steps[target_index]["id"])
        self.phase = "moving"
        if live_err:
            # solve() (below) unconditionally clears the datum, which is
            # right when landing on a group we've fully exited - its targets
            # are recomputed as "what's left from here", so a fresh datum
            # taken right now is exactly the right reference. But if we're
            # only stepping back WITHIN the group we're still inside (one
            # pair done, the other not), that group's datum was captured
            # once at group entry and its targets - per estimate_live_pose -
            # come back unchanged; recapturing the datum here would zero out
            # the progress already made and make an already-achieved pair
            # look like it needs the full original move again.
            old_datum, old_datum_group = dict(self.datum), self.datum_group
            self.current_err = dict(live_err)
            # solve() rebuilds the whole plan and unconditionally resets
            # step_index to 0 - restore the step we actually meant to land
            # on (clamped in case the new plan is somehow shorter).
            self.solve()
            self.step_index = min(target_index, len(self.steps) - 1)
            new_step = self.current_step()
            same_group = new_step is not None and new_step["group"] == old_datum_group
            if old_datum_group and same_group:
                self.datum = old_datum
                self.datum_group = old_datum_group
            self.log("step_back", recomputed=True, step=self.step_index)
            return {"ok": True, "recomputed": True, "step": self.current_step()}
        self.step_index = target_index
        self.log("step_back", recomputed=False, step=self.step_index)
        return {
            "ok": True,
            "recomputed": False,
            "step": self.current_step(),
            "warning": "targets are from the existing plan; re-survey if the "
            "girder has moved since it was computed",
        }

    def undo_survey(self) -> dict:
        """Discard the most recent survey and reopen the upload.

        The usual reason is a wrong file: the operator notices only after the fit
        comes back (perhaps it reported in tolerance when the girder plainly is
        not). Rolling the survey back is safe because nothing has moved — it is a
        data correction, not a machine action. Any moves already made under the
        discarded survey are kept in the log so the record stays truthful.
        """
        if not self.surveys:
            return {"ok": False, "reason": "no survey to undo"}
        dropped = self.surveys.pop()
        self.iteration = max(0, self.iteration - 1)
        self.current_err = self.surveys[-1]["err"] if self.surveys else None
        if not self.surveys:
            self.initial_err = None
        self.plan = None
        self.step_index = 0
        self.phase = "await_survey"
        self.clear_datum()
        if self.status == "complete":
            self.status = "active"
        self.log(
            "survey_undo",
            dropped_iteration=dropped["iteration"],
            filename=dropped.get("filename", ""),
        )
        return {
            "ok": True,
            "dropped": dropped.get("filename") or f"iteration {dropped['iteration']}",
            "phase": self.phase,
        }

    def abandon_iteration(self) -> dict:
        """Honest recovery: drop the plan and go back to survey."""
        self.plan = None
        self.step_index = 0
        self.phase = "await_survey"
        self.clear_datum()
        self.log("abandon_iteration", iteration=self.iteration)
        return {"ok": True, "phase": self.phase}

    def stop(self, reason: str = "") -> dict:
        self.status = "stopped"
        self.log("session_stop", reason=reason)
        return {"ok": True, "status": self.status}

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "serial": self.serial,
            "gtype": self.gtype,
            "operator": self.operator,
            "bay": self.bay,
            "created": self.created,
            "status": self.status,
            "phase": self.phase,
            "iteration": self.iteration,
            "step_index": self.step_index,
            "pairing": self.pairing,
            "first_idx": self.first_idx,
            "combine_vertical": self.combine_vertical,
            "gate_mm": self.gate_mm,
            "heave_bias_mm": self.heave_bias_mm,
            "tol": self.tol,
            "geometry_revision": self.geometry_revision,
            "software_version": self.software_version,
            "temperature": self.temperature,
            "notes": self.notes,
            "datum": self.datum,
            "datum_group": self.datum_group,
            "initial_err": self.initial_err,
            "current_err": self.current_err,
            "plan": self.plan,
            "surveys": self.surveys,
            "moves": self.moves,
            "overrides": self.overrides,
            "events": self.events,
            "reports": self.reports,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        s = cls(
            d["serial"],
            d["gtype"],
            d["operator"],
            pairing=d.get("pairing", "end"),
            first_idx=d.get("first_idx", 0),
            combine_vertical=d.get("combine_vertical", False),
            gate_mm=d.get("gate_mm", DEFAULT_GATE_MM),
            # A record from before this existed has no key here, and its
            # already-computed plan (if any) was necessarily made with no
            # bias -- default to 0.0, not this type's current recommendation,
            # so a resumed old session stays consistent with what it already
            # shows rather than silently reinterpreting it.
            heave_bias_mm=d.get("heave_bias_mm", 0.0),
            bay=d.get("bay", ""),
            sid=d["id"],
        )
        s.created = d.get("created", s.created)
        s.status = d.get("status", "active")
        s.phase = d.get("phase", "await_survey")
        s.iteration = d.get("iteration", 0)
        s.step_index = d.get("step_index", 0)
        s.tol = d.get("tol", s.tol)
        s.temperature = d.get("temperature")
        s.notes = d.get("notes", "")
        s.datum = {k: float(v) for k, v in (d.get("datum") or {}).items()}
        s.datum_group = d.get("datum_group", "")
        s.initial_err = d.get("initial_err")
        s.current_err = d.get("current_err")
        s.plan = d.get("plan")
        s.surveys = d.get("surveys", [])
        s.moves = d.get("moves", [])
        s.overrides = d.get("overrides", [])
        s.events = d.get("events", [])
        s.reports = d.get("reports", [])
        return s

    def state(self) -> dict:
        """Compact state for the UI."""
        step = self.current_step()
        return {
            "id": self.id,
            "serial": self.serial,
            "gtype": self.gtype,
            "operator": self.operator,
            "bay": self.bay,
            "status": self.status,
            "phase": self.phase,
            "iteration": self.iteration,
            "step_index": self.step_index,
            "n_steps": len(self.steps),
            "step": step,
            "steps": [
                {"id": s["id"], "title": s["title"], "index": s["index"]}
                for s in self.steps
            ],
            "pairing": self.pairing,
            "first_idx": self.first_idx,
            "combine_vertical": self.combine_vertical,
            "gate_mm": self.gate_mm,
            "heave_bias_mm": self.heave_bias_mm,
            "tol": self.tol,
            "datum": self.datum,
            "datum_group": self.datum_group,
            "initial_err": self.initial_err,
            "current_err": self.current_err,
            "assessment": (
                G.assess(
                    self.current_err, self.tol, target={"heave": self.heave_bias_mm}
                )
                if self.current_err
                else None
            ),
            "n_surveys": len(self.surveys),
            "n_moves": len(self.moves),
            "reports": self.reports,
            "n_overrides": len(self.overrides),
            "last_survey": self.surveys[-1] if self.surveys else None,
            "geometry_revision": self.geometry_revision,
            "software_version": self.software_version,
            "cross_shift_ratio": (self.plan or {}).get("cross_shift_ratio"),
        }
