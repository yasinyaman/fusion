"""ManualScheduler: jobs run only when the test calls ``tick()``."""

from __future__ import annotations

from collections.abc import Callable


class ManualJob:
    def __init__(self, interval_s: float, fn: Callable[[], None], name: str) -> None:
        self.interval_s = interval_s
        self.fn = fn
        self.name = name
        self.cancelled = False
        self.runs = 0

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        if not self.cancelled:
            self.runs += 1
            self.fn()


class ManualScheduler:
    def __init__(self) -> None:
        self.jobs: list[ManualJob] = []

    def every(self, interval_s: float, fn: Callable[[], None], *, name: str) -> ManualJob:
        job = ManualJob(interval_s, fn, name)
        self.jobs.append(job)
        return job

    def cancel_all(self) -> None:
        for job in self.jobs:
            job.cancel()

    @property
    def active_jobs(self) -> list[ManualJob]:
        return [j for j in self.jobs if not j.cancelled]

    def tick(self) -> None:
        """Run every active job once, as if its interval elapsed."""
        for job in list(self.jobs):
            job.run()

    def job_named(self, name: str) -> ManualJob:
        for job in self.jobs:
            if job.name == name:
                return job
        raise KeyError(name)
