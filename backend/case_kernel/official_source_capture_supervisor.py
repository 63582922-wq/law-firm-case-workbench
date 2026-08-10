"""Bounded desktop supervisor for one-at-a-time official capture work."""
from __future__ import annotations

from threading import Event
from typing import Callable


class OfficialCaptureSupervisor:
    def __init__(self, *, run_once: Callable[[], object | None], stop: Event, interval_seconds: float = 5.0, on_error: Callable[[Exception], None] | None = None) -> None:
        if not 1 <= interval_seconds <= 300:
            raise ValueError("official capture supervisor interval must be 1 to 300 seconds")
        self._run_once, self._stop, self._interval, self._on_error = run_once, stop, interval_seconds, on_error

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as error:
                if self._on_error is not None:
                    self._on_error(error)
            self._stop.wait(self._interval)
