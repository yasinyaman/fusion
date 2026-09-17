"""threading.Timer implementation of the Scheduler port."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class TimerJob:
    """A repeating daemon timer; each run reschedules the next one."""

    def __init__(self, interval_s: float, fn: Callable[[], None], name: str) -> None:
        self._interval = interval_s
        self._fn = fn
        self.name = name
        self._cancelled = threading.Event()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._schedule()

    def _schedule(self) -> None:
        with self._lock:
            if self._cancelled.is_set():
                return
            self._timer = threading.Timer(self._interval, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            self._fn()
        except Exception as e:  # never let a job kill the timer chain
            logger.error("Scheduled job '%s' failed: %s", self.name, e)
        self._schedule()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


class ThreadingScheduler:
    def __init__(self) -> None:
        self._jobs: list[TimerJob] = []
        self._lock = threading.Lock()

    def every(self, interval_s: float, fn: Callable[[], None], *, name: str) -> TimerJob:
        job = TimerJob(interval_s, fn, name)
        with self._lock:
            self._jobs.append(job)
        job.start()
        logger.info("Scheduled '%s' every %ss", name, interval_s)
        return job

    def cancel_all(self) -> None:
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for job in jobs:
            job.cancel()

    @property
    def active_jobs(self) -> list[TimerJob]:
        with self._lock:
            return [j for j in self._jobs if not j.cancelled]
