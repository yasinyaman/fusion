"""Tests for the threading.Timer scheduler."""

import threading
import time

from fusion.adapters.outbound.threading_scheduler import ThreadingScheduler


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestThreadingScheduler:
    def test_runs_repeatedly_until_cancelled(self):
        scheduler = ThreadingScheduler()
        runs = []
        job = scheduler.every(0.02, lambda: runs.append(1), name="tick")
        assert _wait_for(lambda: len(runs) >= 3)
        job.cancel()
        count = len(runs)
        time.sleep(0.1)
        assert len(runs) <= count + 1  # at most one in-flight run after cancel
        assert job.cancelled

    def test_exception_does_not_stop_the_chain(self):
        scheduler = ThreadingScheduler()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first run fails")

        scheduler.every(0.02, flaky, name="flaky")
        assert _wait_for(lambda: len(calls) >= 3)
        scheduler.cancel_all()

    def test_cancel_all_and_active_jobs(self):
        scheduler = ThreadingScheduler()
        event = threading.Event()
        scheduler.every(10, event.set, name="a")
        scheduler.every(10, event.set, name="b")
        assert len(scheduler.active_jobs) == 2
        scheduler.cancel_all()
        assert scheduler.active_jobs == []
        time.sleep(0.05)
        assert not event.is_set()
