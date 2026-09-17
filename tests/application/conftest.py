"""Fixtures for application-layer tests: real DuckDB store, fake sources."""

import pytest

from fusion.application.settings import Settings
from fusion.bootstrap import build_app
from tests.fakes import FakeSourceFactory, ManualScheduler

USERS = [
    {"id": 1, "name": "Alice", "segment": "premium"},
    {"id": 2, "name": "Bob", "segment": "basic"},
    {"id": 3, "name": "Charlie", "segment": "premium"},
    {"id": 4, "name": "Diana", "segment": "standard"},
    {"id": 5, "name": "Eve", "segment": "basic"},
]
ORDERS = [
    {"id": 1, "user_id": 1, "amount": 100.0, "product": "A"},
    {"id": 2, "user_id": 2, "amount": 50.0, "product": "B"},
    {"id": 3, "user_id": 1, "amount": 200.0, "product": "A"},
    {"id": 4, "user_id": 3, "amount": 150.0, "product": "C"},
    {"id": 5, "user_id": 4, "amount": 75.0, "product": "B"},
    {"id": 6, "user_id": 5, "amount": 30.0, "product": "A"},
]
TEST_DB = {"users": USERS, "orders": ORDERS}


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
    """One fake source ``test_db`` with users/orders, both loaded."""
    app.sources.connect("test_db", {"type": "fake", "tables": TEST_DB}, fetch_all=True)
    return app


@pytest.fixture
def app_lazy(app):
    """``warp_main`` fake source with pushdown, metadata only (nothing loaded)."""
    app.sources.connect("warp_main", {"type": "fake", "tables": TEST_DB, "pushdown": True})
    return app
