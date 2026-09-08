"""Girder alignment - web service.

Serves the UI and bridges EPICS. A browser cannot reach Channel Access (raw
UDP/TCP on 5064/5065, and JavaScript has no UDP socket), which is why this
service exists at all. Deployed one instance per build bay.

The alignment maths lives in :mod:`geometry` and is authoritative; this module
only moves data. Never re-implement the numerics in the browser.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider

from . import __version__, epics_io
from . import geometry as G
from . import report as R
from .config import Config
from .session import Session, SessionStore

logger = logging.getLogger(__name__)

PACKAGE = "dls_deb_girder_alignment"

STATIC_DIR = Path(str(resources.files(PACKAGE) / "static"))


class _JSONEncoder(json.JSONEncoder):
    """numpy scalars leak out of the maths layer; Flask cannot serialise them."""

    @staticmethod
    def _clean(o: Any) -> Any:
        """Replace inf/nan with null anywhere in the payload.

        JavaScript's JSON.parse rejects bare Infinity and NaN, so one non-finite
        value anywhere breaks the whole response - and that killed the browser
        poll loop whenever an encoder disconnected.
        """
        if isinstance(o, float):
            return None if (math.isinf(o) or math.isnan(o)) else o
        if isinstance(o, dict):
            return {k: _JSONEncoder._clean(v) for k, v in o.items()}
        if isinstance(o, list | tuple):
            return [_JSONEncoder._clean(v) for v in o]
        return o

    def encode(self, o: Any) -> str:
        return super().encode(self._clean(o))

    def iterencode(self, o: Any, _one_shot: bool = False) -> Any:
        return super().iterencode(self._clean(o), _one_shot)

    def default(self, o: Any) -> Any:
        import numpy as np

        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class _Provider(DefaultJSONProvider):
    def dumps(self, obj: Any, **kw: Any) -> str:
        kw.setdefault("cls", _JSONEncoder)
        kw.setdefault("default", None)
        return json.dumps(obj, **kw)


class Service:
    """Everything one bay's service needs. One girder at a time, by design."""

    def __init__(self, cfg: Config, demo: bool = False):
        self.cfg = cfg
        self.demo = demo
        backend = epics_io.make_backend(demo=demo)
        self.backend = backend
        self.enc = epics_io.EncoderService(
            cfg.pv_map,
            backend,
            cfg.scale,
            zero_setpoint_suffix=cfg.zero_setpoint_suffix,
            zero_process_suffix=cfg.zero_process_suffix,
        )
        self.temp = epics_io.TemperatureService(
            cfg.sensor_pvs, backend, cfg.max_spread_c
        )
        self.store = SessionStore(cfg.sessions_db)
        self.session: Session | None = None
        self.lock = threading.Lock()
        cfg.reports.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Release the session store. See :meth:`SessionStore.close`."""
        self.store.close()

    def __enter__(self) -> Service:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("no active session - start one first")
        return self.session

    def readings(self) -> dict[str, Any]:
        """Read the encoders, capturing a plan datum if a new group has opened.

        Targets are deltas from the state at the start of their move group, so
        the datum must be taken on entering the group. Doing it here, on the
        read path, means it happens exactly once and without an operator having
        to remember a button.
        """
        s = self.session
        if s is None:
            return self.enc.read_all()
        with self.lock:
            step = s.current_step()
            if step is not None and s.needs_datum(step):
                s.capture_datum(self.enc.snapshot(), step["group"])
                self.store.save(s)
            datum = dict(s.datum)
        return self.enc.read_all(datum)


def create_app(service: Service) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR))
    app.json = _Provider(app)
    cfg = service.cfg

    def err(msg: object, code: int = 400) -> tuple:
        return jsonify({"ok": False, "error": str(msg)}), code

    # -- static ----------------------------------------------------------
    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/<path:fn>")
    def static_file(fn: str):
        # 3D models are large and are not shipped in the image; when a models
        # directory is mounted they are served from there, and when it is not
        # the viewer falls back to a plain envelope box on its own.
        if (STATIC_DIR / fn).is_file():
            return send_from_directory(STATIC_DIR, fn)
        if cfg.models and (cfg.models / fn).is_file():
            return send_from_directory(cfg.models, fn)
        return err("not found", 404)

    # -- reference data --------------------------------------------------
    @app.route("/api/info")
    def api_info():
        return jsonify(
            {
                "ok": True,
                "demo": service.demo,
                "software_version": __version__,
                "geometry_revision": G.GEOMETRY_REVISION,
                "live_backend": service.backend.name,
                "domain": cfg.domain,
                "bay": cfg.bay,
                "types": {t: G.machine(t).summary() for t in G.GEOM},
                "axes": [
                    {"key": k, "label": lbl, "unit": u, "default_tol": d}
                    for k, lbl, u, d in G.AXES
                ],
                "encoders": G.ENCODER_IDS,
                "vertical": G.VERT_IDS,
                "horizontal": G.HORZ_IDS,
                "pv_map": cfg.pv_map,
                "sensor_pvs": cfg.sensor_pvs,
                "models_available": bool(cfg.models and cfg.models.is_dir()),
                "limits": {
                    "warp_warn": G.WARP_WARN,
                    "warp_stop": G.WARP_STOP,
                    "stretch_warn": G.STRETCH_WARN,
                    "stretch_stop": G.STRETCH_STOP,
                    "proximity_gate_mm": G.PROXIMITY_GATE_MM,
                },
                "gate_default_mm": cfg.gate_default_mm,
                "n_serials": len(cfg.serials),
                "default_report_dir": str(cfg.reports.resolve()),
            }
        )

    @app.route("/api/temperature")
    def api_temperature():
        return jsonify({"ok": True, **service.temp.read()})

    @app.route("/api/serials")
    def api_serials():
        return jsonify(
            {
                "ok": True,
                "serials": [{"serial": s, **v} for s, v in sorted(cfg.serials.items())],
            }
        )

    @app.route("/api/serial/<serial>")
    def api_serial(serial: str):
        hit = cfg.serials.get(serial.strip().upper())
        if hit:
            return jsonify(
                {
                    "ok": True,
                    "serial": serial,
                    "type": hit["type"],
                    "notes": hit["notes"],
                    "known": True,
                }
            )
        return jsonify(
            {
                "ok": True,
                "serial": serial,
                "type": None,
                "known": False,
                "message": "serial not in lookup table - select the type manually "
                "and add the serial to girder_serials.csv",
            }
        )

    # -- session lifecycle -----------------------------------------------
    @app.route("/api/session", methods=["POST"])
    def api_session_start():
        d = request.get_json(force=True) or {}
        serial = (d.get("serial") or "").strip()
        operator = (d.get("operator") or "").strip()
        gtype = (d.get("type") or "").strip().upper()
        if not serial:
            return err("girder serial is required")
        if not operator:
            return err("operator name is required")
        if gtype not in G.GEOM:
            hit = cfg.serials.get(serial.upper())
            gtype = hit["type"] if hit else ""
        if gtype not in G.GEOM:
            return err("girder type could not be resolved - supply 'type'")

        gate = float(d.get("gate_mm") or cfg.gate_default_mm)
        if gate < cfg.encoder_resolution_mm:
            return err(
                f"gate {gate} mm is finer than encoder resolution "
                f"{cfg.encoder_resolution_mm} mm"
            )

        with service.lock:
            service.session = Session(
                serial=serial,
                gtype=gtype,
                operator=operator,
                bay=cfg.bay or cfg.domain,
                pairing=d.get("pairing", "end"),
                first_idx=int(d.get("first_idx", 0)),
                combine_vertical=bool(d.get("combine_vertical", False)),
                gate_mm=gate,
                tol_overrides=d.get("tol_overrides"),
            )
            service.store.save(service.session)
        return jsonify({"ok": True, "session": service.session.state()})

    @app.route("/api/session", methods=["GET"])
    def api_session_get():
        if service.session is None:
            return jsonify({"ok": True, "session": None})
        return jsonify({"ok": True, "session": service.session.state()})

    @app.route("/api/session/options", methods=["POST"])
    def api_options():
        d = request.get_json(force=True) or {}
        try:
            s = service.require_session()
            with service.lock:
                s.set_options(
                    pairing=d.get("pairing"),
                    first_idx=(
                        int(d["first_idx"]) if d.get("first_idx") is not None else None
                    ),
                    combine_vertical=d.get("combine_vertical"),
                    gate_mm=(
                        float(d["gate_mm"]) if d.get("gate_mm") is not None else None
                    ),
                    tol_overrides=d.get("tol_overrides"),
                )
                service.store.save(s)
            return jsonify({"ok": True, "session": s.state()})
        except Exception as exc:
            return err(exc)

    @app.route("/api/session/stop", methods=["POST"])
    def api_stop():
        d = request.get_json(force=True) or {}
        try:
            s = service.require_session()
            with service.lock:
                s.stop(d.get("reason", ""))
                service.store.save(s)
            out: dict[str, Any] = {"ok": True, "session": s.state()}
            if d.get("report"):
                pdf, js = R.build(s, cfg.reports, partial=True)
                out["report"] = {"pdf": pdf.name, "json": js.name}
            return jsonify(out)
        except Exception as exc:
            return err(exc)

    @app.route("/api/session/close", methods=["POST"])
    def api_close():
        """Free the bay so the next girder can start.

        Discards nothing: the session is already persisted. A session still in
        progress is marked stopped on the way out so it cannot sit in the store
        looking active forever.
        """
        with service.lock:
            if service.session is not None:
                if service.session.status == "active":
                    service.session.stop("closed without completing")
                service.store.save(service.session)
                service.session = None
        return jsonify({"ok": True, "session": None})

    @app.route("/api/sessions")
    def api_sessions():
        return jsonify({"ok": True, "sessions": service.store.list()})

    @app.route("/api/sessions/export")
    def api_sessions_export():
        """Every session in full, for backup.

        The deployment's PersistentVolumeClaim is working storage and is not
        backed up. Reports are pulled off it over HTTP (see
        scripts/mirror_reports.py) and each one carries its own session, so
        this endpoint covers what those miss: sessions that never produced a
        report. Pulling JSON rather than copying sessions.sqlite avoids taking
        a torn copy of a database that is being written to.

        ``sessions`` holds exactly what ``sessions export`` writes on the
        command line.
        """
        sessions = service.store.export()
        return jsonify(
            {
                "ok": True,
                "generated": datetime.now(UTC).isoformat(timespec="seconds"),
                "count": len(sessions),
                "sessions": sessions,
            }
        )

    @app.route("/api/session/resume/<sid>", methods=["POST"])
    def api_resume(sid: str):
        d = service.store.load(sid)
        if not d:
            return err("session not found", 404)
        with service.lock:
            service.session = Session.from_dict(d)
        return jsonify({"ok": True, "session": service.session.state()})

    # -- survey ----------------------------------------------------------
    @app.route("/api/survey", methods=["POST"])
    def api_survey():
        try:
            s = service.require_session()
            fname = ""
            if request.files.get("file"):
                f = request.files["file"]
                fname = f.filename or ""
                pts = parse_points(f.read().decode("utf-8", "replace"))
                temp = request.form.get("temperature")
            else:
                d = request.get_json(force=True) or {}
                pts = d.get("points") or parse_points(d.get("text", ""))
                temp = d.get("temperature")
                fname = d.get("filename", "")
            if len(pts) < 3:
                return err("could not read at least 3 XYZ rows from the survey")

            # Temperature comes from the bay's own sensor average unless the
            # operator typed one in. Recorded for provenance, not compensation.
            detail = None
            if temp in (None, ""):
                detail = service.temp.read()
                temp = detail.get("temperature")
            with service.lock:
                rec = s.add_survey(
                    pts,
                    source="upload",
                    temperature=float(temp) if temp not in (None, "") else None,
                    temperature_detail=detail,
                    filename=fname,
                )
                service.store.save(s)
            return jsonify({"ok": True, "survey": rec, "session": s.state()})
        except Exception as exc:
            return err(exc)

    @app.route("/api/survey/undo", methods=["POST"])
    def api_survey_undo():
        """Discard the last survey so a different file can be loaded.

        Needed even when the fit came back in tolerance - the usual trigger is
        the operator realising the wrong file was uploaded.
        """
        try:
            s = service.require_session()
            with service.lock:
                res = s.undo_survey()
                service.store.save(s)
            res["ok"] = res.get("ok", True)
            res["session"] = s.state()
            return jsonify(res)
        except Exception as exc:
            return err(exc)

    # -- live encoders ---------------------------------------------------
    @app.route("/api/encoders")
    def api_encoders():
        readings = service.readings()
        out: dict[str, Any] = {
            "ok": True,
            "readings": readings,
            "t": datetime.now(UTC).isoformat(),
        }
        s = service.session
        if s is not None:
            vec = [
                (readings.get(e) or {}).get("relative") or 0.0 for e in G.ENCODER_IDS
            ]
            warp, stretch = s.machine.parasitic_of(vec)
            out["parasitic"] = {
                "warp": warp,
                "stretch": stretch,
                "warp_warn": G.WARP_WARN,
                "warp_stop": G.WARP_STOP,
                "stretch_warn": G.STRETCH_WARN,
                "stretch_stop": G.STRETCH_STOP,
            }
            out["datum_group"] = s.datum_group
            step = s.current_step()
            if step:
                out["check"] = s.check_step(readings, step)
        return jsonify(out)

    @app.route("/api/encoders/zero", methods=["POST"])
    def api_zero():
        """Zero the encoders in the IOC. The only write this tool performs.

        Requires an explicit confirmation from the caller, because it is a
        hardware datum change that cannot be undone from here and that everything
        else looking at these PVs will see.
        """
        d = request.get_json(force=True) or {}
        if not d.get("confirm"):
            return err("zeroing the encoders requires confirm=true")
        try:
            with service.lock:
                res = service.enc.zero_encoders(d.get("encoders"))
                s = service.session
                if s is not None:
                    # The absolute readings have just moved, so a plan datum
                    # captured before the zero is meaningless. Dropping it makes
                    # the next encoder poll re-take it against the new frame.
                    s.clear_datum()
                    s.log(
                        "encoder_zero",
                        encoders=res["encoders"],
                        errors=res["errors"],
                        by=d.get("operator", ""),
                    )
                    service.store.save(s)
            if res["errors"]:
                return jsonify(
                    {
                        "ok": False,
                        "error": "some encoders did not zero: "
                        + "; ".join(f"{k} ({v})" for k, v in res["errors"].items()),
                        **res,
                    }
                ), 502
            return jsonify({"ok": True, **res})
        except Exception as exc:
            return err(exc)

    @app.route("/api/demo/drive", methods=["POST"])
    def api_demo_drive():
        """Simulator only: drive encoders toward the current step's targets."""
        if not isinstance(service.backend, epics_io.DemoBackend):
            return err("not running in demo mode")
        d = request.get_json(force=True) or {}
        s = service.session
        targets = d.get("targets")
        step = s.current_step() if s is not None else None
        if targets is None and s is not None and step is not None:
            frac = float(d.get("fraction", 1.0))
            # Drive the FULL rigid state, not just the two active encoders.
            # Moving one pair tilts the girder, so the others must move too;
            # driving only the targets produces a physically impossible reading
            # and trips the parasitic monitors.
            src = step.get("expected") or step["targets"]
            targets = {
                cfg.pv_map[eid]: s.datum.get(eid, 0.0) + val * frac
                for eid, val in src.items()
                if eid in cfg.pv_map
            }
        else:
            targets = {
                cfg.pv_map[k]: v for k, v in (targets or {}).items() if k in cfg.pv_map
            }
        service.backend.set_targets(targets)
        return jsonify({"ok": True, "targets": targets})

    # -- steps -----------------------------------------------------------
    @app.route("/api/step/complete", methods=["POST"])
    def api_step_complete():
        d = request.get_json(force=True) or {}
        try:
            s = service.require_session()
            readings = service.readings()
            with service.lock:
                res = s.complete_step(
                    readings,
                    override=bool(d.get("override")),
                    reason=d.get("reason", ""),
                    operator=d.get("operator", ""),
                )
                service.store.save(s)
            res["ok"] = True
            res["session"] = s.state()
            return jsonify(res)
        except Exception as exc:
            return err(exc)

    @app.route("/api/step/back", methods=["POST"])
    def api_step_back():
        d = request.get_json(force=True) or {}
        try:
            s = service.require_session()
            with service.lock:
                res = s.step_back(live_err=d.get("err"))
                service.store.save(s)
            res["ok"] = res.get("ok", True)
            res["session"] = s.state()
            return jsonify(res)
        except Exception as exc:
            return err(exc)

    @app.route("/api/step/abandon", methods=["POST"])
    def api_abandon():
        try:
            s = service.require_session()
            with service.lock:
                res = s.abandon_iteration()
                service.store.save(s)
            res["session"] = s.state()
            res["ok"] = True
            return jsonify(res)
        except Exception as exc:
            return err(exc)

    # -- reports ---------------------------------------------------------
    @app.route("/api/report", methods=["POST"])
    def api_report():
        d = request.get_json(force=True) or {}
        try:
            s = service.require_session()
            if d.get("notes"):
                s.notes = d["notes"]
            partial = bool(d.get("partial", s.status != "complete"))
            out_dir = Path(d["folder"]).expanduser() if d.get("folder") else cfg.reports
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                probe = out_dir / ".write_test"
                probe.touch()
                probe.unlink()
            except Exception as exc:
                return err(f"cannot write to {out_dir}: {exc}")
            pdf, js = R.build(s, out_dir, partial=partial)
            s.reports.append({"pdf": str(pdf), "json": str(js), "partial": partial})
            s.log("report", pdf=str(pdf), partial=partial)
            service.store.save(s)
            return jsonify(
                {
                    "ok": True,
                    "pdf": pdf.name,
                    "json": js.name,
                    "partial": partial,
                    "dir": str(out_dir.resolve()),
                    "servable": out_dir.resolve() == cfg.reports.resolve(),
                }
            )
        except Exception as exc:
            return err(exc)

    @app.route("/api/reports")
    def api_reports():
        files = sorted((f.name for f in cfg.reports.glob("*.pdf")), reverse=True)
        return jsonify({"ok": True, "reports": files, "dir": str(cfg.reports)})

    @app.route("/reports/<path:fn>")
    def get_report(fn: str):
        return send_from_directory(cfg.reports, fn)

    return app


def parse_points(text: str) -> list[list[float]]:
    """Accept CSV/TSV with an optional name column; take the last three numerics."""
    pts: list[list[float]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.replace("\t", ",").replace(";", ",").split(",")
        nums: list[float] = []
        for p in parts:
            try:
                nums.append(float(p.strip()))
            except ValueError:
                pass
        if len(nums) >= 3:
            pts.append(nums[-3:])
    return pts
