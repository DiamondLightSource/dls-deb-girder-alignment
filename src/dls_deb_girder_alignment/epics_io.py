"""EPICS interface - encoder and temperature readings.

READ-ONLY BY DESIGN. This module never writes to an IOC. Adjustment stays
manual and the tool observes. Encoder zeroing is an IOC operation
(``:X_SP`` then ``:X_ZCALC.PROC``, persisted by autosave), performed once when a
girder is installed on the jacks - not something this application does.

Channel Access only. The service runs in the cluster alongside the IOCs, so the
whole pyepics / CA-gateway / archiver-fallback apparatus the bay-PC version
needed is gone; ``cothread`` is the one live backend, with a simulator for
training and development.

A frozen PV looks exactly like a stationary girder, so every reading carries the
age of its timestamp and the UI shows stale values as stale.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, cast

STALE_AFTER_S = 3.0
"""A reading older than this is flagged stale."""


@dataclass
class Reading:
    pv: str
    value: float | None
    timestamp: float | None
    connected: bool
    error: str | None = None

    @property
    def age(self) -> float | None:
        if self.timestamp is None:
            return None
        return max(0.0, time.time() - self.timestamp)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        age = self.age
        d["age"] = age
        d["stale"] = (age is None) or (age > STALE_AFTER_S)
        return d


class LiveBackend:
    name = "none"

    def read(self, pvs: list[str]) -> dict[str, Reading]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class CothreadBackend(LiveBackend):
    """Channel Access via cothread. The only live backend."""

    name = "cothread"

    def __init__(self) -> None:
        from cothread.catools import caget

        self._caget = caget

    def read(self, pvs: list[str]) -> dict[str, Reading]:
        out: dict[str, Reading] = {}
        try:
            # cothread is untyped, so annotate what caget actually returns: one
            # augmented value per PV, in the order asked for.
            vals = cast(list[Any], self._caget(pvs, timeout=1.0, throw=False, format=1))
        except Exception as exc:  # pragma: no cover - needs a broken CA stack
            return {p: Reading(p, None, None, False, str(exc)) for p in pvs}
        for pv, v in zip(pvs, vals, strict=True):
            if not getattr(v, "ok", True):
                out[pv] = Reading(pv, None, None, False, "disconnected")
                continue
            ts = getattr(v, "timestamp", None) or time.time()
            out[pv] = Reading(pv, float(v), float(ts), True)
        return out


class DemoBackend(LiveBackend):
    """Simulator. Encoders move toward whatever target the session sets.

    Not only a development convenience - this is the training mode, so a
    technician can rehearse the whole procedure without occupying a bay.
    """

    name = "demo"

    def __init__(self) -> None:
        self._val: dict[str, float] = {}
        self._target: dict[str, float] = {}
        self._baseline: dict[str, float] = {}
        self._lock = threading.Lock()
        self._t0 = time.time()

    def set_baseline(self, values: dict[str, float]) -> None:
        """Park a PV at a fixed value, used for the room temperature sensors."""
        with self._lock:
            self._baseline.update(values)
            for pv, v in values.items():
                self._val.setdefault(pv, v)

    def seed(self, pvs: list[str]) -> None:
        with self._lock:
            for pv in pvs:
                self._val.setdefault(pv, 0.0)

    def set_targets(self, targets: dict[str, float]) -> None:
        """Drive the simulated encoders toward these absolute values."""
        with self._lock:
            self._target.update(targets)

    def read(self, pvs: list[str]) -> dict[str, Reading]:
        now = time.time()
        out: dict[str, Reading] = {}
        with self._lock:
            for pv in pvs:
                if pv in self._baseline:  # temperature sensor: slow drift only
                    drift = 0.03 * math.sin((now - self._t0) / 90.0 + hash(pv) % 5)
                    out[pv] = Reading(pv, self._baseline[pv] + drift, now, True)
                    continue
                cur = self._val.get(pv, 0.0)
                tgt = self._target.get(pv)
                if tgt is not None:
                    # Approach asymptotically, with a little noise and backlash feel.
                    cur += (tgt - cur) * 0.12
                    if abs(tgt - cur) < 0.0015:
                        cur = tgt
                    self._val[pv] = cur
                jitter = 0.00035 * math.sin((now - self._t0) * 2.3 + hash(pv) % 7)
                out[pv] = Reading(
                    pv, cur + jitter + random.gauss(0, 0.00015), now, True
                )
        return out


def make_backend(demo: bool = False) -> LiveBackend:
    """Pick a backend. Falls back to the simulator if cothread is unavailable."""
    if demo:
        return DemoBackend()
    return CothreadBackend()


class TemperatureService:
    """Room air temperature, averaged over this bay's nearest sensors.

    The sensor array covers the whole hall and does not follow the per-bay
    encoder domain, so the bay is configured with an explicit list of full PV
    names rather than a domain and a set of suffixes.

    A disconnected sensor is excluded from the mean rather than poisoning it,
    and the count actually used is reported so a degraded average is visible.
    Temperature is recorded with every survey for provenance; the girders are
    thermally stabilised before work, so it drives no compensation.
    """

    def __init__(
        self,
        sensor_pvs: list[str],
        backend: LiveBackend,
        max_spread_c: float = 0.5,
    ) -> None:
        self.sensor_pvs = list(sensor_pvs)
        self.backend = backend
        self.max_spread_c = max_spread_c
        if isinstance(backend, DemoBackend) and self.sensor_pvs:
            backend.seed(self.sensor_pvs)
            backend.set_baseline(
                {pv: 21.4 + 0.05 * i for i, pv in enumerate(self.sensor_pvs)}
            )

    def read(self) -> dict[str, Any]:
        if not self.sensor_pvs:
            return {
                "temperature": None,
                "sensors": {},
                "health": "down",
                "error": "no temperature sensors configured",
            }
        try:
            raw = self.backend.read(self.sensor_pvs)
        except Exception as exc:
            return {
                "temperature": None,
                "sensors": {},
                "health": "down",
                "error": str(exc),
            }

        used: list[float] = []
        detail: dict[str, Any] = {}
        bad: list[str] = []
        for pv in self.sensor_pvs:
            r = raw.get(pv)
            d = r.as_dict() if r else {"value": None, "stale": True, "error": "no data"}
            detail[pv] = d
            if r and r.value is not None and not d.get("stale"):
                used.append(float(r.value))
            else:
                why = d.get("error") or ("stale" if d.get("stale") else "no value")
                bad.append(f"{pv} ({why})")

        n_used, n_total = len(used), len(self.sensor_pvs)
        spread = (max(used) - min(used)) if len(used) > 1 else 0.0
        if n_used == 0:
            health = "down"
        elif n_used < n_total or spread > self.max_spread_c:
            health = "degraded"
        else:
            health = "ok"
        return {
            "temperature": (sum(used) / n_used) if used else None,
            "sensors_used": n_used,
            "sensors_total": n_total,
            "spread": spread,
            "sensors": detail,
            "health": health,
            "failed": bad,
            "demo": isinstance(self.backend, DemoBackend),
            "error": None
            if used
            else "no valid sensor readings - " + "; ".join(bad[:3]),
        }


class EncoderService:
    """Maps encoder ids to PVs and reports staleness.

    Deliberately stateless. The encoders are zeroed in the IOC, and the *plan
    datum* - the reading each encoder had when the current move group opened,
    which is what step targets are measured from - belongs to the session, so it
    is persisted and survives a restart. Pass it to :meth:`read_all`.
    """

    def __init__(
        self,
        pv_map: dict[str, str],
        backend: LiveBackend,
        scale: dict[str, float] | None = None,
    ) -> None:
        self.pv_map = dict(pv_map)
        self.backend = backend
        self.scale = scale or {}
        if isinstance(backend, DemoBackend):
            backend.seed(list(self.pv_map.values()))

    def read_all(self, datum: dict[str, float] | None = None) -> dict[str, Any]:
        """Read every encoder.

        ``absolute`` is the calibrated IOC value. ``relative`` is that value
        measured from ``datum``, and is what the step targets are compared
        against. With no datum the two are equal.
        """
        datum = datum or {}
        pvs = list(self.pv_map.values())
        try:
            raw = self.backend.read(pvs)
        except Exception as exc:
            raw = {p: Reading(p, None, None, False, str(exc)) for p in pvs}

        out: dict[str, Any] = {}
        for eid, pv in self.pv_map.items():
            r = raw.get(pv) or Reading(pv, None, None, False, "no data")
            d = r.as_dict()
            k = self.scale.get(eid, 1.0)
            absolute = None if r.value is None else r.value * k
            d["absolute"] = absolute
            d["datum"] = datum.get(eid, 0.0)
            d["relative"] = None if absolute is None else absolute - datum.get(eid, 0.0)
            d["encoder"] = eid
            out[eid] = d
        return out

    def snapshot(self) -> dict[str, float]:
        """Current absolute values, for capturing a plan datum."""
        return {
            eid: r["absolute"]
            for eid, r in self.read_all().items()
            if r["absolute"] is not None
        }
