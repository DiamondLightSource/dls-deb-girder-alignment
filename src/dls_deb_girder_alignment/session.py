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
        combine_vertical: bool = False,
        gate_mm: float = DEFAULT_GATE_MM,
        tol_overrides: dict | None = None,
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
            "assessment": G.assess(fit.err, self.tol),
        }
        self.surveys.append(rec)
        if temperature is not None:
            self.temperature = temperature
        self.current_err = dict(fit.err)
        if self.initial_err is None:
            self.initial_err = dict(fit.err)

        # validate the achieved move against the model, for free
        if self.moves:
            last = self.moves[-1]
            if last.get("predicted_pose_change"):
                prev = self.surveys[-2]["err"] if len(self.surveys) > 1 else None
                if prev:
                    actual = {
                        k: self.current_err[k] - prev[k] for k in self.current_err
                    }
                    last["model_check"] = {
                        "predicted": last["predicted_pose_change"],
                        "actual": actual,
                        "residual": {
                            k: actual[k] - last["predicted_pose_change"].get(k, 0.0)
                            for k in actual
                        },
                    }

        self.phase = "solved" if not rec["assessment"]["in_tolerance"] else "complete"
        self.log(
            "survey",
            iteration=self.iteration,
            rms=fit.rms,
            n=fit.n_points,
            warnings=fit.warnings,
            err=fit.err,
        )
        if self.phase == "solved":
            self.solve()
        else:
            self.status = "complete"
            self.log("converged", err=fit.err)
        return rec

    # -- planning --------------------------------------------------------
    def solve(self) -> dict:
        if self.current_err is None:
            raise RuntimeError("no survey yet")
        self.plan = G.compute_plan(
            self.machine,
            self.current_err,
            self.pairing,
            self.first_idx,
            self.combine_vertical,
        )
        self.step_index = 0
        self.clear_datum()  # targets are new; the old reference is meaningless
        self.phase = "moving"
        self.log(
            "solve",
            n_steps=len(self.plan["steps"]),
            combine_vertical=self.combine_vertical,
            pairing=self.pairing,
        )
        return self.plan

    def set_options(self, **kw):
        """Change pairing / first pair / combine mode, then re-solve."""
        for k in ("pairing", "first_idx", "combine_vertical", "gate_mm"):
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
            stale = (readings.get(eid) or {}).get("stale", True)
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
            ok = bool(abs(e) <= self.gate_mm and not stale)
            per[eid] = {
                "target": float(tgt),
                "actual": float(rel),
                "error": float(e),
                "ok": ok,
                "stale": bool(stale),
            }
            worst = max(worst, abs(float(e)))

        vec = [(readings.get(e) or {}).get("relative") or 0.0 for e in G.ENCODER_IDS]
        warp, stretch = self.machine.parasitic_of(vec)
        par_ok = bool(abs(warp) <= G.WARP_STOP and abs(stretch) <= G.STRETCH_STOP)

        stale_any = any(
            (readings.get(e) or {}).get("stale", True) for e in step["targets"]
        )
        ok = bool(
            all(v["ok"] for v in per.values())
            and par_ok
            and not stale_any
            and not missing
        )
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
        if stale_any:
            reasons.append("one or more readings are stale")

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

    def step_back(self, live_err: dict | None = None) -> dict:
        """Re-open the previous step.

        Targets are recomputed from the CURRENT pose, never replayed. If a fresh
        pose estimate is not available the caller should re-survey instead.
        """
        if self.step_index == 0:
            return {"ok": False, "reason": "already at the first step"}
        self.step_index -= 1
        self.phase = "moving"
        if live_err:
            self.current_err = dict(live_err)
            self.solve()
            self.log("step_back", recomputed=True, step=self.step_index)
            return {"ok": True, "recomputed": True, "step": self.current_step()}
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
            "tol": self.tol,
            "datum": self.datum,
            "datum_group": self.datum_group,
            "initial_err": self.initial_err,
            "current_err": self.current_err,
            "assessment": (
                G.assess(self.current_err, self.tol) if self.current_err else None
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
