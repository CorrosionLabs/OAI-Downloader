from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import Event, Lock, Thread
from typing import Callable
from uuid import uuid4


class WebError(Exception):
    """A public error code, safe to send to the browser."""


class JobManager:
    """One writer for the process-global data root and its JSON files."""

    def __init__(self):
        self._operation = Lock()
        self._state = Lock()
        self._jobs: dict[str, dict] = {}
        self._stop_events: dict[str, Event] = {}

    @contextmanager
    def exclusive(self):
        if not self._operation.acquire(blocking=False):
            raise WebError("busy")
        try:
            yield
        finally:
            self._operation.release()

    def snapshot(self, kind: str | None = None):
        with self._state:
            if kind is None:
                return deepcopy(list(self._jobs.values()))
            return deepcopy(self._jobs.get(kind, {"kind": kind, "status": "idle"}))

    def clear(self):
        # Caller holds the operation lock.
        with self._state:
            self._jobs.clear()
            self._stop_events.clear()

    def request_stop(self, kind: str) -> dict:
        with self._state:
            job = self._jobs.get(kind)
            if not job or job["status"] != "running":
                raise WebError("not_running")
            self._stop_events[kind].set()
            job["stop_requested"] = True
            return deepcopy(job)

    def start(self, kind: str, operation: Callable) -> dict:
        if not self._operation.acquire(blocking=False):
            raise WebError("busy")
        stop_event = Event()
        job = {"id": uuid4().hex, "kind": kind, "status": "running",
               "phase": "starting", "current": 0, "total": 0,
               "result": None, "error": None, "stop_requested": False}
        with self._state:
            self._jobs[kind] = job
            self._stop_events[kind] = stop_event

        def update(**values):
            with self._state:
                job.update(values)

        def run():
            try:
                result = operation(update, stop_event.is_set)
                update(result=result, status="partial" if result.get("partial") else "completed",
                       phase="completed")
            except WebError as exc:
                update(status="error", phase="error", error=str(exc))
            except Exception:
                # Core transport exceptions can contain response bodies or credentials.
                update(status="error", phase="error", error="operation_failed")
            finally:
                self._operation.release()

        initial = deepcopy(job)
        try:
            Thread(target=run, daemon=True, name=f"oai-{kind}").start()
        except Exception:
            update(status="error", error="operation_failed")
            self._operation.release()
            raise
        return initial
