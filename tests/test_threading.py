"""Channel Access calls must survive being made from a WSGI worker thread.

cothread's scheduler is cooperative and belongs to the thread that started it.
Calling into it from anywhere else asserts outright - and every request the web
server handles arrives on a worker thread, so without marshalling, live mode
reports every PV as errored no matter how healthy the IOC is. Demo mode never
touches cothread, so nothing else in the suite would notice.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from dls_deb_girder_alignment import epics_io


@pytest.fixture
def backend() -> epics_io.CothreadBackend:
    """A real backend, with the cothread module swapped for a recorder.

    Constructing it stamps the owner thread, which is what the dispatch turns
    on. The scheduler is never actually run, so the stub stands in for it.
    """
    b = epics_io.CothreadBackend()
    b._cothread = _Recorder()  # type: ignore[assignment]
    return b


class _Recorder:
    """Stands in for the cothread module, recording what was marshalled."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def CallbackResult(self, fn: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        self.calls.append((fn, args))
        return fn(*args, **kwargs)


def test_the_owning_thread_calls_straight_through(backend):
    """Marshalling on the scheduler's own thread would wait on itself for ever."""
    assert backend._call(lambda: "value") == "value"
    assert backend._cothread.calls == []


def test_a_worker_thread_is_marshalled_onto_the_scheduler(backend):
    """This is the path every web request takes."""
    out: list[Any] = []

    def worker() -> None:
        out.append(backend._call(lambda: "value"))

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert out == ["value"]
    assert len(backend._cothread.calls) == 1


def test_arguments_reach_the_marshalled_call(backend):
    """caget is called with pvs, timeout, throw and format - none may be lost."""
    out: list[Any] = []

    def worker() -> None:
        out.append(backend._call(lambda *a, **k: (a, k), 1, 2, timeout=3.0))

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert out == [((1, 2), {"timeout": 3.0})]


def test_the_owner_is_the_thread_that_built_the_backend():
    """It is constructed at start-up, before the server takes a thread."""
    idents: list[int] = []

    def build() -> None:
        idents.append(epics_io.CothreadBackend()._owner)

    t = threading.Thread(target=build)
    t.start()
    t.join()

    assert idents[0] != threading.get_ident()
