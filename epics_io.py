"""
EPICS interface — encoder readings.

READ-ONLY BY DESIGN. This module never writes to an IOC. Adjustment stays manual;
the tool observes. Do not add a caput path.

Two independent sources:

  LIVE   — what the technician follows during a move. Needs sub-second latency, so
           it must come from Channel Access, never from the archiver (which is
           deadbanded and decimated).
           Backends tried in order: cothread.catools -> pyepics -> aa.ca -> demo.

  ARCHIVE— Diamond `aapy` (pip install aapy, imports as `aa`). Used for the move
           trace in the report, so the PDF shows what the encoders actually did
           during the move rather than just the endpoints.
               from aa.js import JsonFetcher
               JsonFetcher(host, port).get_values(pv, start_utc, end_utc)
                   -> object with .utc_datetimes and .values

A frozen PV looks exactly like a stationary girder, so every reading carries the
age of its timestamp and the UI is expected to show stale values as stale.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

STALE_AFTER_S = 3.0          # reading older than this is flagged stale


@dataclass
class Reading:
    pv: str
    value: Optional[float]
    timestamp: Optional[float]      # epoch seconds
    connected: bool
    error: Optional[str] = None

    @property
    def age(self) -> Optional[float]:
        return None if self.timestamp is None else max(0.0, time.time() - self.timestamp)

    def as_dict(self) -> dict:
        d = asdict(self)
        a = self.age
        d["age"] = a
        d["stale"] = (a is None) or (a > STALE_AFTER_S)
        return d


# --------------------------------------------------------------------------
# Live backends
# --------------------------------------------------------------------------
class LiveBackend:
    name = "none"

    def read(self, pvs: List[str]) -> Dict[str, Reading]:
        raise NotImplementedError

    def close(self):
        pass


class CothreadBackend(LiveBackend):
    name = "cothread"

    def __init__(self):
        from cothread.catools import caget, connect      # noqa: F401
        self._caget = caget

    def read(self, pvs):
        out = {}
        try:
            vals = self._caget(pvs, timeout=1.0, throw=False, format=1)
        except Exception as exc:                          # pragma: no cover
            return {p: Reading(p, None, None, False, str(exc)) for p in pvs}
        for p, v in zip(pvs, vals):
            ok = getattr(v, "ok", True)
            if not ok:
                out[p] = Reading(p, None, None, False, "disconnected")
                continue
            ts = getattr(v, "timestamp", None) or time.time()
            out[p] = Reading(p, float(v), float(ts), True)
        return out


class PyepicsBackend(LiveBackend):
    name = "pyepics"

    def __init__(self):
        import epics
        self._epics = epics
        self._pvs: Dict[str, object] = {}

    def _pv(self, name):
        if name not in self._pvs:
            pv = self._epics.PV(name, auto_monitor=True)
            # A freshly created PV is not connected yet; without this wait the
            # first poll after startup reports every channel as dead.
            pv.wait_for_connection(timeout=2.0)
            self._pvs[name] = pv
        return self._pvs[name]

    def read(self, pvs):
        out = {}
        for p in pvs:
            pv = self._pv(p)
            if not pv.connected or pv.value is None:
                out[p] = Reading(p, None, None, False, "disconnected")
                continue
            out[p] = Reading(p, float(pv.value),
                             float(pv.timestamp or time.time()), True)
        return out

    def close(self):
        for pv in self._pvs.values():
            try:
                pv.disconnect()
            except Exception:
                pass


class AaCaBackend(LiveBackend):
    name = "aa.ca"

    def __init__(self):
        from aa.ca import CaFetcher                      # noqa: F401
        self._f = CaFetcher()

    def read(self, pvs):
        now = datetime.now(timezone.utc)
        out = {}
        for p in pvs:
            try:
                d = self._f.get_values(p, now - timedelta(seconds=5), now)
                if len(d.values):
                    out[p] = Reading(p, float(d.values[-1]),
                                     d.utc_datetimes[-1].timestamp(), True)
                else:
                    out[p] = Reading(p, None, None, False, "no samples")
            except Exception as exc:
                out[p] = Reading(p, None, None, False, str(exc))
        return out


class DemoBackend(LiveBackend):
    """Simulator. Encoders move toward whatever target the session sets.

    This is not just for offline development — it is the training mode, so a
    technician can rehearse the whole procedure without occupying a bay.
    """
    name = "demo"

    def __init__(self):
        self._val: Dict[str, float] = {}
        self._target: Dict[str, float] = {}
        self._baseline: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._t0 = time.time()

    def set_baseline(self, values: Dict[str, float]):
        """Park a PV at a fixed value (used for the room temperature sensors)."""
        with self._lock:
            self._baseline.update(values)
            for pv, v in values.items():
                self._val.setdefault(pv, v)

    def seed(self, pvs: List[str]):
        with self._lock:
            for p in pvs:
                self._val.setdefault(p, 0.0)

    def set_targets(self, targets: Dict[str, float]):
        """Drive the simulated encoders toward these values."""
        with self._lock:
            self._target.update(targets)

    def nudge(self, pv: str, delta: float):
        with self._lock:
            self._val[pv] = self._val.get(pv, 0.0) + delta

    def read(self, pvs):
        now = time.time()
        out = {}
        with self._lock:
            for p in pvs:
                if p in self._baseline:      # temperature sensor: slow drift only
                    v = self._baseline[p] + 0.03 * math.sin((now - self._t0) / 90.0
                                                            + hash(p) % 5)
                    out[p] = Reading(p, v, now, True)
                    continue
                cur = self._val.get(p, 0.0)
                tgt = self._target.get(p)
                if tgt is not None:
                    # approach asymptotically, with a little noise + backlash feel
                    cur += (tgt - cur) * 0.12
                    if abs(tgt - cur) < 0.0015:
                        cur = tgt
                    self._val[p] = cur
                jitter = 0.00035 * math.sin((now - self._t0) * 2.3 + hash(p) % 7)
                out[p] = Reading(p, cur + jitter + random.gauss(0, 0.00015), now, True)
        return out


def make_live_backend(prefer: str = "auto", archiver_host: str = "",
                      archiver_port: int = 80) -> LiveBackend:
    """Pick a live source.

    'archiver' is never chosen automatically: it is a degraded fallback for a PC
    that can reach the archiver but not the IOCs, and it must be an explicit
    choice so nobody ends up following a move on decimated data by accident.
    """
    order = ([prefer] if prefer not in ("auto", None) else
             ["cothread", "pyepics", "aa.ca"])
    errors = []
    for name in order:
        try:
            if name == "demo":
                return DemoBackend()
            if name == "cothread":
                return CothreadBackend()
            if name == "pyepics":
                return PyepicsBackend()
            if name == "aa.ca":
                return AaCaBackend()
            if name == "archiver":
                return ArchiverBackend(archiver_host, archiver_port)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("no live EPICS backend available -> " + "; ".join(errors)
                       + ".  Install pyepics (Windows/Linux) or cothread (Linux), "
                       "or run with --demo.")


# --------------------------------------------------------------------------
# Archiver (Diamond aapy)
# --------------------------------------------------------------------------
class Archiver:
    """Historical traces via aapy. Optional: absence must not stop a session."""

    def __init__(self, host: str = "localhost", port: int = 80, demo: bool = False):
        self.host, self.port, self.demo = host, port, demo
        self._fetcher = None
        self.error: Optional[str] = None
        if not demo:
            try:
                from aa.js import JsonFetcher
                self._fetcher = JsonFetcher(host, port)
            except ImportError:
                self.error = ("aapy not installed — 'pip install aapy' "
                              "(installs as aapy, imports as aa)")
            except TypeError as exc:
                # protobuf 4.x+ cannot load aapy's older generated code
                if "Descriptors cannot be created directly" in str(exc):
                    self.error = ('protobuf version clash: aapy needs protobuf '
                                  '3.20.x. Fix with: pip install "protobuf<3.21"')
                else:
                    self.error = f"archiver init failed: {exc}"
            except Exception as exc:
                self.error = f"archiver init failed: {exc}"

    @property
    def available(self) -> bool:
        return self.demo or self._fetcher is not None

    def _fetch(self, pv: str, start: datetime, end: datetime):
        """Raw (datetime, float) pairs from the archiver.

        Two things bite here and both are handled:
          * aapy wants its own utc_datetime objects, not plain datetimes.
          * data.values comes back as a numpy array whose elements may each be
            1-element arrays for scalar PVs, so a bare float(v) raises
            "only 0-dimensional arrays can be converted to Python scalars".
            Flattening first is what makes it work.
        """
        from aa.utils import utc_datetime
        import numpy as np

        s = start.astimezone(timezone.utc)
        e = end.astimezone(timezone.utc)
        su = utc_datetime(s.year, s.month, s.day, s.hour, s.minute)
        eu = utc_datetime(e.year, e.month, e.day, e.hour, e.minute)
        data = self._fetcher.get_values(pv, su, eu)
        if data is None or len(data.values) == 0:
            return []
        values = np.asarray(data.values).flatten()
        out = []
        for t, v in zip(data.utc_datetimes, values):
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append((t, float(v)))
        return out

    def trace(self, pv: str, start: datetime, end: datetime) -> List[List[float]]:
        """[[epoch_seconds, value], ...] over the window."""
        if self.demo or self._fetcher is None:
            n = 24
            span = (end - start).total_seconds()
            return [[start.timestamp() + span * i / n,
                     math.sin(i / 4.0) * 0.05] for i in range(n + 1)]
        try:
            return [[t.timestamp(), v] for t, v in self._fetch(pv, start, end)]
        except Exception as exc:
            self.error = f"{pv}: {exc}"
            return []

    def latest(self, pvs: List[str], lookback_s: int = 120) -> Dict[str, Reading]:
        """Most recent archived sample per PV — used by ArchiverBackend."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=max(60, lookback_s))
        out = {}
        for pv in pvs:
            if self._fetcher is None:
                out[pv] = Reading(pv, None, None, False, self.error or "no archiver")
                continue
            try:
                pts = self._fetch(pv, start, end)
                if pts:
                    t, v = pts[-1]
                    out[pv] = Reading(pv, v, t.timestamp(), True)
                else:
                    out[pv] = Reading(pv, None, None, False, "no recent samples")
            except Exception as exc:
                out[pv] = Reading(pv, None, None, False, str(exc))
        return out


class ArchiverBackend(LiveBackend):
    """Read 'live' values from the Archiver Appliance instead of Channel Access.

    LAST RESORT. Use it when the PC can reach the archiver but not the IOCs —
    which is common for an office machine outside the controls network.

    It is NOT a substitute for CA when following a move: archived data is
    deadbanded and decimated, arrives seconds late, and a value that has not
    changed enough to be archived simply is not there. The tool's staleness
    display will show that honestly, and the move screen will feel sluggish.
    Get CA access sorted before doing real alignment work.
    """
    name = "archiver"

    def __init__(self, host: str, port: int, lookback_s: int = 120):
        self.arch = Archiver(host, port)
        if not self.arch.available:
            raise RuntimeError(self.arch.error or "archiver unavailable")
        self.lookback_s = lookback_s

    def read(self, pvs: List[str]) -> Dict[str, Reading]:
        return self.arch.latest(pvs, self.lookback_s)


# --------------------------------------------------------------------------
# Encoder service: PV mapping, display zeroing, staleness
# --------------------------------------------------------------------------
class TemperatureService:
    """Room air temperature for the bay, averaged from the nearest sensors.

    Bays overlap in the sensor array, so each bay averages a different subset:
    the mapping lives in config (bays: {1: [1,2,3], ...}) alongside the sensor PVs.
    Temperature is recorded with every survey — the girders are thermally
    stabilised before work, so this is provenance rather than live compensation.

    A sensor that is disconnected is excluded from the mean rather than poisoning
    it, and the count actually used is reported so a degraded average is visible.
    """

    def __init__(self, sensor_pvs: Dict[str, str], bays: Dict[str, List],
                 backend: LiveBackend):
        self.sensor_pvs = {str(k): v for k, v in (sensor_pvs or {}).items()}
        self.bays = {str(k): [str(s) for s in v] for k, v in (bays or {}).items()}
        self.backend = backend
        if isinstance(backend, DemoBackend) and self.sensor_pvs:
            backend.seed(list(self.sensor_pvs.values()))
            backend.set_baseline({pv: 21.4 + 0.05 * i
                                  for i, pv in enumerate(self.sensor_pvs.values())})

    @property
    def bay_ids(self) -> List[str]:
        return sorted(self.bays, key=lambda x: (len(x), x))

    def read_bay(self, bay: str) -> dict:
        bay = str(bay)
        sensors = self.bays.get(bay, [])
        if not sensors:
            return {"bay": bay, "temperature": None, "sensors": {},
                    "error": f"bay {bay!r} not configured"}
        pvs = [self.sensor_pvs[s] for s in sensors if s in self.sensor_pvs]
        if not pvs:
            return {"bay": bay, "temperature": None, "sensors": {},
                    "error": "no sensor PVs configured for this bay"}
        try:
            raw = self.backend.read(pvs)
        except Exception as exc:
            return {"bay": bay, "temperature": None, "sensors": {}, "error": str(exc)}

        used, detail, bad = [], {}, []
        for s in sensors:
            pv = self.sensor_pvs.get(s)
            r = raw.get(pv) if pv else None
            d = r.as_dict() if r else {"value": None, "stale": True,
                                       "error": "not configured"}
            d["pv"] = pv
            detail[s] = d
            if r and r.value is not None and not d.get("stale"):
                used.append(float(r.value))
            else:
                why = (d.get("error") or ("stale" if d.get("stale") else "no value"))
                bad.append(f"{s} ({why})")

        n_used, n_tot = len(used), len(sensors)
        if n_used == 0:
            health = "down"
        elif n_used < n_tot:
            health = "degraded"
        else:
            health = "ok"
        spread = (max(used) - min(used)) if len(used) > 1 else 0.0
        if health == "ok" and spread > 0.5:
            health = "degraded"          # air is not as uniform as it should be
        return {
            "bay": bay, "sensors_used": n_used, "sensors_total": n_tot,
            "temperature": (sum(used) / n_used) if used else None,
            "spread": spread, "sensors": detail, "health": health,
            "failed": bad, "demo": isinstance(self.backend, DemoBackend),
            "error": None if used else
                     "no valid sensor readings — " + "; ".join(bad[:3]),
        }


class EncoderService:
    """Maps encoder ids -> PVs, applies display zero offsets, reports staleness.

    Zeroing is a DISPLAY OFFSET ONLY and never a hardware zero. Absolute values
    stay available alongside relative ones because the absolute reading is what
    proves the +/-6 mm travel budget.
    """

    def __init__(self, pv_map: Dict[str, str], backend: LiveBackend,
                 scale: Optional[Dict[str, float]] = None,
                 bay_pv_map: Optional[Dict[str, Dict[str, str]]] = None):
        self.default_pv_map = dict(pv_map)
        self.bay_pv_map = {str(k): dict(v)
                           for k, v in (bay_pv_map or {}).items()}
        self.pv_map = dict(pv_map)
        self.bay: str = ""
        self.backend = backend
        self.scale = scale or {}
        self.zero: Dict[str, float] = {}
        self._last: Dict[str, Reading] = {}
        if isinstance(backend, DemoBackend):
            backend.seed(self._all_pvs())

    def _all_pvs(self) -> List[str]:
        pvs = set(self.default_pv_map.values())
        for m in self.bay_pv_map.values():
            pvs.update(m.values())
        return sorted(pvs)

    def set_bay(self, bay: str) -> dict:
        """Each build bay has its own encoder rig, so its own PV set.

        Switching bay also drops any display zero: the datum belonged to the
        previous bay's encoders and would be meaningless against these.
        """
        bay = str(bay or "")
        self.bay = bay
        self.pv_map = dict(self.bay_pv_map.get(bay) or self.default_pv_map)
        self.zero.clear()
        return {"bay": bay, "pv_map": self.pv_map,
                "source": "bay" if bay in self.bay_pv_map else "default"}

    def read_all(self) -> Dict[str, dict]:
        pvs = [self.pv_map[e] for e in self.pv_map]
        try:
            raw = self.backend.read(pvs)
        except Exception as exc:
            raw = {p: Reading(p, None, None, False, str(exc)) for p in pvs}
        out = {}
        for eid, pv in self.pv_map.items():
            r = raw.get(pv) or Reading(pv, None, None, False, "no data")
            if r.value is not None:
                self._last[eid] = r
            d = r.as_dict()
            k = self.scale.get(eid, 1.0)
            absolute = None if r.value is None else r.value * k
            d["absolute"] = absolute
            d["zero"] = self.zero.get(eid, 0.0)
            d["relative"] = (None if absolute is None
                             else absolute - self.zero.get(eid, 0.0))
            d["encoder"] = eid
            out[eid] = d
        return out

    def set_zero(self, encoder_ids: Optional[List[str]] = None):
        """Capture current absolute values as the display datum."""
        cur = self.read_all()
        ids = encoder_ids or list(self.pv_map)
        for e in ids:
            a = cur.get(e, {}).get("absolute")
            if a is not None:
                self.zero[e] = a
        return {e: self.zero.get(e, 0.0) for e in ids}

    def clear_zero(self, encoder_ids: Optional[List[str]] = None):
        for e in (encoder_ids or list(self.pv_map)):
            self.zero.pop(e, None)

    def snapshot(self) -> Dict[str, Optional[float]]:
        cur = self.read_all()
        return {e: cur[e]["absolute"] for e in cur}
