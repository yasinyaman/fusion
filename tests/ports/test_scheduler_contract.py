"""Contract every Scheduler implementation must honour."""

import pytest

from fusion.adapters.outbound.threading_scheduler import ThreadingScheduler
from tests.fakes import ManualScheduler


@pytest.fixture(params=[ManualScheduler, ThreadingScheduler], ids=["manual", "threading"])
def scheduler(request):
    s = request.param()
    yield s
    s.cancel_all()


class TestSchedulerContract:
    def test_every_returns_cancellable_job(self, scheduler):
        job = scheduler.every(100, lambda: None, name="j")
        assert hasattr(job, "cancel")
        job.cancel()

    def test_cancel_all(self, scheduler):
        scheduler.every(100, lambda: None, name="a")
        scheduler.every(100, lambda: None, name="b")
        scheduler.cancel_all()
        assert scheduler.active_jobs == []
