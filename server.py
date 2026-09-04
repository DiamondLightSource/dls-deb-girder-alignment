"""
Girder alignment — local web service.

Runs on the bay PC, serves the UI and bridges EPICS. The browser cannot reach
Channel Access (raw UDP/TCP on 5064/5065, no UDP from JavaScript), which is why
this service exists at all.

The alignment maths lives in geometry.py and is authoritative; this module only
moves data. Never re-implement the numerics in the browser.

    python server.py --demo          # simulator: training / off-site
    python server.py                 # live EPICS
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import geometry as G
import epics_io
import report as R
from session import Session, SessionStore, DEFAULT_GATE_MM

HERE = Path(__file__).parent
app = Flask(__name__, static_folder=str(HERE / "static"))


class _NumpyJSON(json.JSONEncoder):
    """numpy scalars leak out of the maths layer; Flask cannot serialise them."""

    @staticmethod
    def _clean(o):
        """Replace inf/nan with null anywhere in the payload.

        JavaScript's JSON.parse rejects bare Infinity and NaN, so a single
        non-finite value anywhere would break the whole response.
        """
        import math as _m
        if isinstance(o, float):
            return None if (_m.isinf(o) or _m.isnan(o)) else o
        if isinstance(o, dict):
            return {k: _NumpyJSON._clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_NumpyJSON._clean(v) for v in o]
        return o

    def encode(self, o):
        return super().encode(self._clean(o))

    def iterencode(self, o, _one_shot=False):
        return super().iterencode(self._clean(o), _one_shot)

    def default(self, o):
        import numpy as _np
        if isinstance(o, _np.bool_):
            return bool(o)
        if isinstance(o, _np.integer):
            return int(o)
        if isinstance(o, _np.floating):
            return float(o)
        if isinstance(o, _np.ndarray):
            return o.tolist()
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


app.json_encoder = _NumpyJSON          # Flask < 2.3
try:                                   # Flask >= 2.3 uses a provider
    from flask.json.provider import DefaultJSONProvider

    class _Provider(DefaultJSONProvider):
        def dumps(self, obj, **kw):
            kw.setdefault("cls", _NumpyJSON)
            kw.setdefault("default", None)
            return json.dumps(obj, **kw)

    app.json = _Provider(app)
except Exception:                      # pragma: no cover
    pass

CFG: dict = {}
STORE = None            # type: ignore[assignment]
ENC = None              # type: ignore[assignment]
TEMP = None             # type: ignore[assignment]
ARCHIVER = None         # type: ignore[assignment]
SERIALS: dict = {}
SESSION: Session | None = None
LOCK = threading.Lock()
DEMO = False


# --------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    text = path.read_text()
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        # Minimal fallback so the tool runs without PyYAML installed.
        cfg, stack = {}, [(-1, cfg)]
        for raw in text.splitlines():
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            key, _, val = raw.strip().partition(":")
            val = val.split("#")[0].strip().strip('"').strip("'")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if val in ("", "{}"):
                node = {}
                parent[key.strip()] = node
                stack.append((indent, node))
            else:
                try:
                    val = float(val) if "." in val else int(val)
                except ValueError:
                    pass
                parent[key.strip()] = val
        return cfg


def load_serials(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    with path.open() as f:
        for row in csv.DictReader(r for r in f if not r.lstrip().startswith("#")):
            s = (row.get("serial") or "").strip()
            t = (row.get("type") or "").strip().upper()
            if s and t in G.GEOM:
                out[s.upper()] = {"type": t, "notes": (row.get("notes") or "").strip()}
    return out


def _require_session() -> Session:
    if SESSION is None:
        raise RuntimeError("no active session — start one first")
    return SESSION


def _err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:fn>")
def static_file(fn):
    return send_from_directory(app.static_folder, fn)


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
@app.route("/api/info")
def api_info():
    return jsonify({
        "ok": True,
        "demo": DEMO,
        "software_version": __import__("session").SOFTWARE_VERSION,
        "geometry_revision": G.GEOMETRY_REVISION,
        "live_backend": ENC.backend.name,
        "archiver_available": ARCHIVER.available,
        "archiver_error": ARCHIVER.error,
        "types": {t: G.machine(t).summary() for t in G.GEOM},
        "axes": [{"key": k, "label": l, "unit": u, "default_tol": d}
                 for k, l, u, d in G.AXES],
        "encoders": G.ENCODER_IDS,
        "vertical": G.VERT_IDS, "horizontal": G.HORZ_IDS,
        "pv_map": ENC.pv_map,
        "bay_pv_map": ENC.bay_pv_map,
        "limits": {"warp_warn": G.WARP_WARN, "warp_stop": G.WARP_STOP,
                   "stretch_warn": G.STRETCH_WARN, "stretch_stop": G.STRETCH_STOP,
                   "travel_mm": G.TRAVEL_LIMIT_MM,
                   "proximity_gate_mm": G.PROXIMITY_GATE_MM},
        "gate_default_mm": CFG.get("gate", {}).get("default_mm", DEFAULT_GATE_MM),
        "n_serials": len(SERIALS),
        "bays": TEMP.bay_ids,
        "bay_sensors": TEMP.bays,
        "default_report_dir": str((HERE / CFG["paths"]["reports"]).resolve()),
    })


@app.route("/api/temperature")
def api_temperature():
    bay = request.args.get("bay") or (SESSION.bay if SESSION else "")
    if not bay:
        return jsonify({"ok": True, "bay": None, "temperature": None,
                        "error": "no bay selected"})
    return jsonify({"ok": True, **TEMP.read_bay(bay)})


@app.route("/api/serials")
def api_serials():
    return jsonify({"ok": True,
                    "serials": [{"serial": s, **v} for s, v in sorted(SERIALS.items())]})


@app.route("/api/serial/<serial>")
def api_serial(serial):
    hit = SERIALS.get(serial.strip().upper())
    if hit:
        return jsonify({"ok": True, "serial": serial, "type": hit["type"],
                        "notes": hit["notes"], "known": True})
    return jsonify({"ok": True, "serial": serial, "type": None, "known": False,
                    "message": "serial not in lookup table — select the type manually "
                               "and add the serial to girder_serials.csv"})


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------
@app.route("/api/session", methods=["POST"])
def api_session_start():
    global SESSION
    d = request.get_json(force=True) or {}
    serial = (d.get("serial") or "").strip()
    operator = (d.get("operator") or "").strip()
    gtype = (d.get("type") or "").strip().upper()
    if not serial:
        return _err("girder serial is required")
    if not operator:
        return _err("operator name is required")
    if gtype not in G.GEOM:
        hit = SERIALS.get(serial.upper())
        gtype = hit["type"] if hit else ""
    if gtype not in G.GEOM:
        return _err("girder type could not be resolved — supply 'type'")

    gate = float(d.get("gate_mm") or CFG.get("gate", {}).get("default_mm",
                                                            DEFAULT_GATE_MM))
    res = float(CFG.get("gate", {}).get("encoder_resolution_mm", 0.001))
    if gate < res:
        return _err(f"gate {gate} mm is finer than encoder resolution {res} mm")

    with LOCK:
        ENC.set_bay(str(d.get("bay") or ""))     # each bay has its own encoder rig
        SESSION = Session(serial=serial, gtype=gtype, operator=operator,
                          bay=str(d.get("bay") or ""),
                          pairing=d.get("pairing", "end"),
                          first_idx=int(d.get("first_idx", 0)),
                          combine_vertical=bool(d.get("combine_vertical", False)),
                          gate_mm=gate,
                          tol_overrides=d.get("tol_overrides"))
        ENC.clear_zero()
        STORE.save(SESSION)
    return jsonify({"ok": True, "session": SESSION.state()})


@app.route("/api/session", methods=["GET"])
def api_session_get():
    if SESSION is None:
        return jsonify({"ok": True, "session": None})
    return jsonify({"ok": True, "session": SESSION.state()})


@app.route("/api/session/options", methods=["POST"])
def api_options():
    d = request.get_json(force=True) or {}
    try:
        s = _require_session()
        with LOCK:
            s.set_options(pairing=d.get("pairing"),
                          first_idx=(int(d["first_idx"])
                                     if d.get("first_idx") is not None else None),
                          combine_vertical=d.get("combine_vertical"),
                          gate_mm=(float(d["gate_mm"])
                                   if d.get("gate_mm") is not None else None),
                          tol_overrides=d.get("tol_overrides"))
            STORE.save(s)
        return jsonify({"ok": True, "session": s.state()})
    except Exception as exc:
        return _err(exc)


@app.route("/api/session/stop", methods=["POST"])
def api_stop():
    d = request.get_json(force=True) or {}
    try:
        s = _require_session()
        with LOCK:
            s.stop(d.get("reason", ""))
            STORE.save(s)
        out = {"ok": True, "session": s.state()}
        if d.get("report"):
            pdf, js = R.build(s, HERE / CFG["paths"]["reports"], partial=True)
            out["report"] = {"pdf": pdf.name, "json": js.name}
        return jsonify(out)
    except Exception as exc:
        return _err(exc)


@app.route("/api/session/close", methods=["POST"])
def api_close():
    """Clear the active session slot so a new girder can be started.

    The session itself is already persisted, so this discards nothing — it only
    frees the bay. A session still in progress is marked stopped on the way out
    so it cannot sit in the store looking active forever.
    """
    global SESSION
    with LOCK:
        if SESSION is not None:
            if SESSION.status == "active":
                SESSION.stop("closed without completing")
            STORE.save(SESSION)
            SESSION = None
        ENC.clear_zero()
    return jsonify({"ok": True, "session": None})


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"ok": True, "sessions": STORE.list()})


@app.route("/api/session/resume/<sid>", methods=["POST"])
def api_resume(sid):
    global SESSION
    d = STORE.load(sid)
    if not d:
        return _err("session not found", 404)
    with LOCK:
        SESSION = Session.from_dict(d)
        ENC.set_bay(SESSION.bay)
    return jsonify({"ok": True, "session": SESSION.state()})


# --------------------------------------------------------------------------
# Survey
# --------------------------------------------------------------------------
def _parse_points(text: str):
    """Accept CSV/TSV with optional name column; take the last three numerics."""
    pts = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p for p in line.replace("\t", ",").replace(";", ",").split(",")]
        nums = []
        for p in parts:
            try:
                nums.append(float(p.strip()))
            except ValueError:
                pass
        if len(nums) >= 3:
            pts.append(nums[-3:])
    return pts


@app.route("/api/survey", methods=["POST"])
def api_survey():
    try:
        s = _require_session()
        fname = ""
        if request.files.get("file"):
            f = request.files["file"]
            fname = f.filename or ""
            pts = _parse_points(f.read().decode("utf-8", "replace"))
            temp = request.form.get("temperature")
        else:
            d = request.get_json(force=True) or {}
            pts = d.get("points") or _parse_points(d.get("text", ""))
            temp = d.get("temperature")
            fname = d.get("filename", "")
        if len(pts) < 3:
            return _err("could not read at least 3 XYZ rows from the survey")

        # Temperature comes from the bay's own sensor average unless the operator
        # typed one in. Recorded for provenance, not used for compensation.
        detail = None
        if temp in (None, ""):
            if s.bay:
                detail = TEMP.read_bay(s.bay)
                temp = detail.get("temperature")
            else:
                temp = None
        with LOCK:
            rec = s.add_survey(pts, source="upload",
                               temperature=float(temp) if temp not in (None, "") else None,
                               temperature_detail=detail, filename=fname)
            STORE.save(s)
        return jsonify({"ok": True, "survey": rec, "session": s.state()})
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------
# Live encoders
# --------------------------------------------------------------------------
@app.route("/api/encoders")
def api_encoders():
    readings = ENC.read_all()
    out = {"ok": True, "readings": readings, "t": datetime.now(timezone.utc).isoformat()}
    vec = [(readings.get(e) or {}).get("relative") or 0.0 for e in G.ENCODER_IDS]
    if SESSION is not None:
        warp, stretch = SESSION.machine.parasitic_of(vec)
        out["parasitic"] = {"warp": warp, "stretch": stretch,
                            "warp_warn": G.WARP_WARN, "warp_stop": G.WARP_STOP,
                            "stretch_warn": G.STRETCH_WARN,
                            "stretch_stop": G.STRETCH_STOP}
        step = SESSION.current_step()
        if step:
            out["check"] = SESSION.check_step(readings, step)
            out["travel"] = SESSION.travel_check(readings, step)
    return jsonify(out)


@app.route("/api/encoders/zero", methods=["POST"])
def api_zero():
    d = request.get_json(force=True) or {}
    ids = d.get("encoders")
    if d.get("clear"):
        ENC.clear_zero(ids)
        act = "cleared"
        z = {}
    else:
        z = ENC.set_zero(ids)
        act = "set"
    if SESSION is not None:
        SESSION.log("encoder_zero", action=act, encoders=ids or "all", values=z)
        STORE.save(SESSION)
    return jsonify({"ok": True, "action": act, "zero": z})


@app.route("/api/demo/drive", methods=["POST"])
def api_demo_drive():
    """Simulator only: drive encoders toward the current step's targets."""
    if not isinstance(ENC.backend, epics_io.DemoBackend):
        return _err("not running in demo mode")
    d = request.get_json(force=True) or {}
    s = SESSION
    targets = d.get("targets")
    if targets is None and s and s.current_step():
        frac = float(d.get("fraction", 1.0))
        step = s.current_step()
        # Drive the FULL rigid state, not just the two active encoders. Moving one
        # pair tilts the girder, so the other vertical encoders must move too;
        # driving only the targets would produce a physically impossible reading
        # and trip the parasitic monitors.
        src = step.get("expected") or step["targets"]
        targets = {ENC.pv_map[eid]: ENC.zero.get(eid, 0.0) + val * frac
                   for eid, val in src.items() if eid in ENC.pv_map}
    else:
        targets = {ENC.pv_map[k]: v for k, v in (targets or {}).items()}
    ENC.backend.set_targets(targets)
    return jsonify({"ok": True, "targets": targets})


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
@app.route("/api/step/complete", methods=["POST"])
def api_step_complete():
    d = request.get_json(force=True) or {}
    try:
        s = _require_session()
        readings = ENC.read_all()
        with LOCK:
            res = s.complete_step(readings,
                                  override=bool(d.get("override")),
                                  reason=d.get("reason", ""),
                                  operator=d.get("operator", ""))
            STORE.save(s)
        res["ok"] = True
        res["session"] = s.state()
        return jsonify(res)
    except Exception as exc:
        return _err(exc)


@app.route("/api/step/back", methods=["POST"])
def api_step_back():
    d = request.get_json(force=True) or {}
    try:
        s = _require_session()
        with LOCK:
            res = s.step_back(live_err=d.get("err"))
            STORE.save(s)
        res["ok"] = res.get("ok", True)
        res["session"] = s.state()
        return jsonify(res)
    except Exception as exc:
        return _err(exc)


@app.route("/api/survey/undo", methods=["POST"])
def api_survey_undo():
    """Discard the last survey so a different file can be loaded.

    Needed even when the fit came back in tolerance — the usual trigger is the
    operator realising the wrong report was uploaded.
    """
    try:
        s = _require_session()
        with LOCK:
            res = s.undo_survey()
            STORE.save(s)
        res["ok"] = res.get("ok", True)
        res["session"] = s.state()
        return jsonify(res)
    except Exception as exc:
        return _err(exc)


@app.route("/api/step/abandon", methods=["POST"])
def api_abandon():
    try:
        s = _require_session()
        with LOCK:
            res = s.abandon_iteration()
            STORE.save(s)
        res["session"] = s.state()
        res["ok"] = True
        return jsonify(res)
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
@app.route("/api/report", methods=["POST"])
def api_report():
    d = request.get_json(force=True) or {}
    try:
        s = _require_session()
        if d.get("notes"):
            s.notes = d["notes"]
        partial = bool(d.get("partial", s.status != "complete"))
        out_dir = Path(d["folder"]).expanduser() if d.get("folder") else \
            (HERE / CFG["paths"]["reports"])
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / ".write_test"
            probe.touch()
            probe.unlink()
        except Exception as exc:
            return _err(f"cannot write to {out_dir}: {exc}")
        pdf, js = R.build(s, out_dir, partial=partial)
        if not hasattr(s, "reports"):
            s.reports = []
        s.reports.append({"pdf": str(pdf), "json": str(js), "partial": partial})
        s.log("report", pdf=str(pdf), partial=partial)
        STORE.save(s)
        local = out_dir.resolve() == (HERE / CFG["paths"]["reports"]).resolve()
        return jsonify({"ok": True, "pdf": pdf.name, "json": js.name,
                        "partial": partial, "dir": str(out_dir.resolve()),
                        "servable": local})
    except Exception as exc:
        return _err(exc)


@app.route("/api/reports")
def api_reports():
    out_dir = HERE / CFG["paths"]["reports"]
    files = sorted((f.name for f in out_dir.glob("*.pdf")), reverse=True)
    return jsonify({"ok": True, "reports": files, "dir": str(out_dir)})


@app.route("/reports/<path:fn>")
def get_report(fn):
    return send_from_directory(HERE / CFG["paths"]["reports"], fn)


# --------------------------------------------------------------------------
def main():
    global CFG, STORE, ENC, TEMP, ARCHIVER, SERIALS, DEMO
    ap = argparse.ArgumentParser(description="Girder alignment service")
    ap.add_argument("--demo", action="store_true",
                    help="simulated encoders — training and off-site use")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--serials", default=str(HERE / "girder_serials.csv"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--open", action="store_true", help="open a browser tab")
    args = ap.parse_args()

    DEMO = args.demo
    CFG = load_config(Path(args.config))
    SERIALS = load_serials(Path(args.serials))

    ep = CFG.get("epics", {})
    backend_name = "demo" if DEMO else ep.get("live_backend", "auto")
    try:
        backend = (epics_io.DemoBackend() if backend_name == "demo"
                   else epics_io.make_live_backend(
                       backend_name, ep.get("archiver_host", ""),
                       int(ep.get("archiver_port", 80))))
    except Exception as exc:
        print(f"[epics] {exc}\n[epics] falling back to demo encoders", file=sys.stderr)
        backend = epics_io.DemoBackend()
        DEMO = True

    ENC = epics_io.EncoderService(ep.get("pv_map", {}), backend,
                                  ep.get("scale") or {},
                                  ep.get("bay_pv_map") or {})
    tc = CFG.get("temperature", {}) or {}
    TEMP = epics_io.TemperatureService(tc.get("sensor_pvs", {}),
                                       tc.get("bays", {}), backend)
    ARCHIVER = epics_io.Archiver(ep.get("archiver_host", "localhost"),
                                 int(ep.get("archiver_port", 80)), demo=DEMO)
    STORE = SessionStore(HERE / CFG["paths"]["sessions_db"])
    (HERE / CFG["paths"]["reports"]).mkdir(parents=True, exist_ok=True)

    url = f"http://{'localhost' if args.host in ('0.0.0.0','127.0.0.1') else args.host}:{args.port}/"
    print("=" * 62)
    print(f"  Girder Alignment  ·  {'DEMO' if DEMO else 'LIVE'} mode")
    print(f"  live backend : {backend.name}")
    print(f"  archiver     : {'ok' if ARCHIVER.available else ARCHIVER.error}")
    print(f"  geometry rev : {G.GEOMETRY_REVISION}")
    print(f"  serials      : {len(SERIALS)} in lookup")
    print(f"  bays         : {', '.join(TEMP.bay_ids) or 'none configured'}")
    print(f"  reports      : {HERE / CFG['paths']['reports']}")
    print(f"  open         : {url}")
    print("=" * 62)
    if args.open:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
