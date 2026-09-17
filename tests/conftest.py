"""Shared fixtures: a FusionApp on a real in-memory DuckDB with fake sources.

- ``app``           empty app, ManualScheduler, FakeSourceFactory
- ``app_with_data`` ``test_db`` source (users/orders) fully loaded
- ``app_lazy``      ``warp_main`` pushdown-capable source, metadata only
- ``e2e_app``       real WarpSource over a FakeWarpTransport (mock Warp API)
"""

import pytest

from fusion.adapters.outbound.warp.source import WarpSource
from fusion.application.settings import Settings
from fusion.bootstrap import build_app
from tests.data import MOCK_DB, TEST_DB
from tests.fakes import FakeSourceFactory, FakeWarpTransport, ManualScheduler

WARP_URL = "http://localhost:8080"


@pytest.fixture
def scheduler():
    return ManualScheduler()


@pytest.fixture
def factory():
    return FakeSourceFactory()


@pytest.fixture
def settings(tmp_path):
    return Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path / "backups"))


@pytest.fixture
def app(settings, scheduler, factory):
    a = build_app(settings, scheduler=scheduler, source_factory=factory)
    yield a
    a.close()


@pytest.fixture
def app_with_data(app):
    app.sources.connect("test_db", {"type": "fake", "tables": TEST_DB}, fetch_all=True)
    return app


@pytest.fixture
def app_lazy(app):
    app.sources.connect("warp_main", {"type": "fake", "tables": TEST_DB, "pushdown": True})
    return app


@pytest.fixture
def e2e_transport():
    return FakeWarpTransport(MOCK_DB, database="ecommerce")


def _e2e_app(settings, scheduler, transport):
    def factory(name, config):
        return WarpSource.from_config(name, {**config, "transport": transport})

    a = build_app(settings, scheduler=scheduler, source_factory=factory)
    a.sources.connect("ecommerce", {"type": "warp", "base_url": WARP_URL, "database": "ecommerce"})
    return a


@pytest.fixture
def e2e_app(settings, scheduler, e2e_transport):
    """Full stack: FakeWarpTransport -> WarpSource -> FusionApp (lazy, nothing loaded)."""
    a = _e2e_app(settings, scheduler, e2e_transport)
    yield a
    a.close()


@pytest.fixture
def denying_transport():
    """A Warp 0.10 whose ``/query/execute`` answers 403 (raw query off)."""
    return FakeWarpTransport(MOCK_DB, database="ecommerce", deny_raw_query=True)


@pytest.fixture
def denying_app(settings, scheduler, denying_transport):
    a = _e2e_app(settings, scheduler, denying_transport)
    yield a
    a.close()


@pytest.fixture
def legacy_transport():
    """A Warp 0.9: no capabilities, raw query off, single DB served un-prefixed."""
    return FakeWarpTransport(
        MOCK_DB, database="ecommerce", mode="legacy", raw_query=False, single_db_unprefixed=True
    )


@pytest.fixture
def legacy_app(settings, scheduler, legacy_transport):
    a = _e2e_app(settings, scheduler, legacy_transport)
    yield a
    a.close()
