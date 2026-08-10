"""Bounded desktop supervisor for one-at-a-time official capture work."""
from __future__ import annotations

from threading import Event
from typing import Callable


class OfficialCaptureSupervisor:
    def __init__(self, *, run_once: Callable[[], object | None], stop: Event, interval_seconds: float = 5.0) -> None:
        if not 1 <= interval_seconds <= 300:
            raise ValueError("official capture supervisor interval must be 1 to 300 seconds")
        self._run_once, self._stop, self._interval = run_once, stop, interval_seconds

    def run(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            self._stop.wait(self._interval)
