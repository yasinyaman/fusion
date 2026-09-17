"""Tests for SourceService."""

import pytest

from fusion.domain.errors import ConnectionError
from fusion.domain.models import TableRef
from tests.data import ORDERS, TEST_DB, USERS


class TestConnect:
    def test_connect_registers_metadata_only(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        assert app.catalog.source_names() == ["db"]
        assert app.catalog.get_table("db.users").column_names == {"id", "name", "segment"}
        assert app.catalog.get_table("db.users").row_count == 5
        assert not app.catalog.is_loaded("db.users")
        assert factory.sources["db"].calls_named("fetch_table") == []
        assert app.sources.has_sources
        assert app.sources.source("db") is factory.sources["db"]

    def test_connect_creates_store_schema(self, app):
        app.sources.connect("db", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        app.sources.ensure_loaded(["db.t"])
        assert app.store.count("db.t") == 1

    def test_fetch_all_loads_everything(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB}, fetch_all=True)
        assert app.catalog.is_loaded("db.users")
        assert app.catalog.is_loaded("db.orders")
        assert len(factory.sources["db"].calls_named("fetch_table")) == 2

    def test_invalid_source_name(self, app):
        with pytest.raises(ConnectionError, match="Invalid source name"):
            app.sources.connect("bad.name", {"type": "fake"})
        with pytest.raises(ConnectionError):
            app.sources.connect("drop;", {"type": "fake"})

    def test_missing_type(self, app):
        with pytest.raises(ConnectionError, match="type"):
            app.sources.connect("db", {})

    def test_connect_failure_propagates(self, app):
        with pytest.raises(ConnectionError, match="refused"):
            app.sources.connect("db", {"type": "fake", "fail_connect": True})
        assert not app.sources.has_sources

    def test_disconnect(self, app_with_data, factory):
        app_with_data.sources.disconnect("test_db")
        assert not app_with_data.catalog.has_source("test_db")
        assert factory.sources["test_db"].closed
        assert not app_with_data.sources.has_sources
        with pytest.raises(Exception):  # noqa: B017 - schema dropped, any store error
            app_with_data.store.count("test_db.users")


class TestEnsureLoaded:
    def test_loads_once_and_marks(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        newly = app.sources.ensure_loaded(["db.users", TableRef("db", "users")])
        assert newly == [TableRef("db", "users")]
        assert app.catalog.is_loaded("db.users")
        assert app.store.count("db.users") == 5
        assert app.sources.ensure_loaded(["db.users"]) == []
        assert len(factory.sources["db"].calls_named("fetch_table")) == 1

    def test_unknown_source_is_skipped(self, app):
        assert app.sources.ensure_loaded(["ghost.t"]) == []

    def test_ingest_cap_forwarded(self, settings, scheduler, factory):
        from dataclasses import replace

        from fusion.bootstrap import build_app

        capped = build_app(
            replace(settings, max_ingest_rows=2), scheduler=scheduler, source_factory=factory
        )
        try:
            capped.sources.connect("db", {"type": "fake", "tables": TEST_DB})
            capped.sources.ensure_loaded(["db.users"])
            assert factory.sources["db"].calls_named("fetch_table") == [("fetch_table", "users", 2)]
            assert capped.store.count("db.users") == 2
            assert capped.catalog.get_table("db.users").row_count == 2
        finally:
            capped.close()

    def test_table_stats(self, app):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        app.sources.ensure_loaded(["db.users"])
        assert app.sources.table_stats() == {"db.users": 5, "db.orders": -1}


class TestPushdownSource:
    def test_returns_source_only_when_capable(self, app):
        app.sources.connect("plain", {"type": "fake", "tables": TEST_DB})
        app.sources.connect("pushy", {"type": "fake", "tables": TEST_DB, "pushdown": True})
        assert app.sources.pushdown_source("plain") is None
        assert app.sources.pushdown_source("pushy") is not None
        assert app.sources.pushdown_source("missing") is None


class TestRefresh:
    def test_refresh_reloads_only_loaded_tables(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        app.sources.ensure_loaded(["db.users"])
        source = factory.sources["db"]
        source.set_table("users", USERS + [{"id": 6, "name": "Zed", "segment": "basic"}])
        source.set_table("orders", ORDERS[:1])
        source.calls.clear()

        app.sources.refresh_all()
        assert app.store.count("db.users") == 6
        assert app.catalog.get_table("db.users").row_count == 6
        assert app.catalog.get_table("db.orders").row_count == 1  # metadata refreshed
        assert not app.catalog.is_loaded("db.orders")
        assert source.calls_named("fetch_table") == [("fetch_table", "users", None)]

    def test_refresh_failure_logged_unless_forced(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        factory.sources["db"].discover_schema = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        app.sources.refresh_all()  # swallowed
        with pytest.raises(RuntimeError):
            app.sources.refresh_all(force=True)

    def test_auto_refresh_uses_scheduler(self, app, scheduler, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        app.sources.start_auto_refresh(interval=10)
        assert app.sources.auto_refresh_running
        job = scheduler.job_named("auto-refresh")
        assert job.interval_s == 10
        factory.sources["db"].calls.clear()
        scheduler.tick()
        assert factory.sources["db"].calls_named("discover_schema")
        app.sources.stop_auto_refresh()
        assert job.cancelled
        assert not app.sources.auto_refresh_running

    def test_close_all(self, app, factory):
        app.sources.connect("a", {"type": "fake", "tables": TEST_DB})
        app.sources.connect("b", {"type": "fake", "tables": TEST_DB})
        app.sources.close_all()
        assert factory.sources["a"].closed and factory.sources["b"].closed
        assert not app.sources.has_sources
