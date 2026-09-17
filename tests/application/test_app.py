"""Tests for FusionApp and build_app."""

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.adapters.outbound.memory_cache import MemoryQueryCache
from fusion.adapters.outbound.threading_scheduler import ThreadingScheduler
from fusion.application.app import FusionApp
from fusion.application.settings import Settings
from fusion.bootstrap import build_app
from tests.fakes import FakeSourceFactory, ManualScheduler


class TestBuildApp:
    def test_defaults_produce_working_app(self):
        with build_app(Settings(threads=1, memory_limit="256MB")) as app:
            assert isinstance(app, FusionApp)
            assert isinstance(app.store, DuckDBStore)
            assert isinstance(app.cache, MemoryQueryCache)
            assert isinstance(app.scheduler, ThreadingScheduler)
            assert app.query.sql("SELECT 41 + 1 AS x").rows == [(42,)]
            assert app.tools.execute("cache_stats", {})["cached_queries"] == 1

    def test_default_settings_when_omitted(self):
        app = build_app()
        try:
            assert app.settings == Settings()
        finally:
            app.close()

    def test_overrides_are_used(self, tmp_path):
        scheduler = ManualScheduler()
        cache = MemoryQueryCache(max_entries=1, default_ttl=1)
        factory = FakeSourceFactory()
        app = build_app(
            Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path)),
            scheduler=scheduler,
            cache=cache,
            source_factory=factory,
            clock=lambda: 123.0,
        )
        try:
            assert app.scheduler is scheduler
            assert app.cache is cache
            app.sources.connect("db", {"type": "fake", "tables": {"t": [{"a": 1}]}})
            assert "db" in factory.sources
            spec = app.views.create("v", "SELECT 1")
            assert spec.created_at == 123.0
        finally:
            app.close()

    def test_close_is_idempotent_and_stops_jobs(self, tmp_path):
        scheduler = ManualScheduler()
        app = build_app(Settings(threads=1, memory_limit="256MB"), scheduler=scheduler)
        app.views.create("v", "SELECT 1", refresh="hourly")
        app.close()
        assert scheduler.active_jobs == []
        app.close()  # second close must not raise

    def test_schema_context(self, app_with_data):
        context = app_with_data.schema_context()
        assert "test_db" in context
        assert "users" in context
        assert "5 rows" in context
        assert app_with_data.schema_context(["other"]) == "No schemas available."
