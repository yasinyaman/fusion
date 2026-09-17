"""Outbound port: periodic job scheduling (view refresh, backups, ...)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ScheduledJob(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    def every(self, interval_s: float, fn: Callable[[], None], *, name: str) -> ScheduledJob:
        """Run ``fn`` every ``interval_s`` seconds until the job is cancelled."""
        ...

    def cancel_all(self) -> None: ...
