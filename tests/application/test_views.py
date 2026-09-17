"""Tests for MaterializedViewService."""

import pytest

from fusion.domain.errors import GuardrailViolation, QueryError


class TestLifecycle:
    def test_create_and_list(self, app_with_data):
        spec = app_with_data.views.create(
            "test_view", "SELECT segment, COUNT(*) AS cnt FROM test_db.users GROUP BY segment"
        )
        assert spec.table_name == "mv_test_view"
        views = app_with_data.views.list_views()
        assert len(views) == 1
        assert views[0]["name"] == "test_view"
        assert views[0]["refresh"] == "manual"
        assert app_with_data.views.has("test_view")

    def test_rows(self, app_with_data):
        app_with_data.views.create("v", "SELECT COUNT(*) AS cnt FROM test_db.users")
        result = app_with_data.views.rows("v")
        assert result.to_records() == [{"cnt": 5}]

    def test_refresh(self, app_with_data, factory):
        app_with_data.views.create("v", "SELECT COUNT(*) AS cnt FROM test_db.users")
        factory.sources["test_db"].set_table("users", [{"id": 1, "name": "x", "segment": "s"}])
        app_with_data.sources.refresh_all()
        app_with_data.views.refresh("v")
        assert app_with_data.views.rows("v").to_records() == [{"cnt": 1}]

    def test_drop(self, app_with_data):
        app_with_data.views.create("v", "SELECT 1 AS x")
        app_with_data.views.drop("v")
        assert app_with_data.views.list_views() == []
        with pytest.raises(QueryError):
            app_with_data.store.count("mv_v")

    def test_nonexistent(self, app_with_data):
        with pytest.raises(QueryError):
            app_with_data.views.rows("nope")
        with pytest.raises(QueryError):
            app_with_data.views.refresh("nope")
        with pytest.raises(QueryError):
            app_with_data.views.drop("nope")

    def test_invalid_name_and_sql(self, app_with_data):
        with pytest.raises(QueryError, match="Invalid view name"):
            app_with_data.views.create("bad.name", "SELECT 1")
        with pytest.raises(GuardrailViolation):
            app_with_data.views.create("v", "DROP TABLE test_db.users")
        with pytest.raises(QueryError, match="Failed to create"):
            app_with_data.views.create("v", "SELECT * FROM does_not_exist")
        assert app_with_data.views.list_views() == []

    def test_describe(self, app_with_data):
        app_with_data.views.create(
            "v", "SELECT product, SUM(amount) AS total FROM test_db.orders GROUP BY product"
        )
        info = app_with_data.views.describe("mv_v")
        assert info["table"] == "mv_v"
        assert [c["name"] for c in info["columns"]] == ["product", "total"]
        assert info["row_count"] == 3
        with pytest.raises(QueryError):
            app_with_data.views.describe("mv_missing")


class TestLazyLoad:
    def test_create_view_lazy_loads_unloaded_table(self, app, factory):
        """Regression: a view over a never-queried table must load it first."""
        app.sources.connect("db", {"type": "fake", "tables": {"orders": [{"n": 1}, {"n": 2}]}})
        assert not app.catalog.is_loaded("db.orders")
        app.views.create("v", "SELECT SUM(n) AS s FROM db.orders")
        assert app.catalog.is_loaded("db.orders")
        assert app.views.rows("v").rows == [(3,)]

    def test_refresh_after_disconnect_reload(self, app):
        app.sources.connect("db", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        app.views.create("v", "SELECT a FROM db.t")
        app.catalog.mark_unloaded("db.t")
        app.views.refresh("v")  # ensure_loaded runs again without error
        assert app.catalog.is_loaded("db.t")


class TestScheduledRefresh:
    def test_hourly_schedules_job(self, app_with_data, scheduler):
        app_with_data.views.create(
            "v", "SELECT COUNT(*) AS cnt FROM test_db.users", refresh="hourly"
        )
        job = scheduler.job_named("mv:v")
        assert job.interval_s == 3600
        before = app_with_data.views.get("v").last_refresh
        app_with_data.views.get("v").last_refresh = before - 100
        scheduler.tick()
        assert app_with_data.views.get("v").last_refresh >= before
        app_with_data.views.drop("v")
        assert job.cancelled

    def test_unknown_interval_is_manual(self, app_with_data, scheduler):
        app_with_data.views.create("v", "SELECT 1", refresh="whenever")
        assert scheduler.jobs == []

    def test_close_cancels_jobs(self, app_with_data, scheduler):
        app_with_data.views.create("v", "SELECT 1", refresh="every 5 minutes")
        app_with_data.views.close()
        assert scheduler.active_jobs == []

    def test_refresh_all_by_priority(self, app_with_data):
        app_with_data.views.create("low", "SELECT 1", priority="low")
        app_with_data.views.create("crit", "SELECT 1", priority="critical")
        app_with_data.views.refresh_all()
        assert {v["name"] for v in app_with_data.views.list_views()} == {"low", "crit"}


class TestCacheInvalidation:
    def test_creating_a_view_clears_the_cache(self, app_with_data):
        sql = "SELECT COUNT(*) AS n FROM test_db.orders"
        app_with_data.query.sql(sql)
        assert app_with_data.query.sql(sql).from_cache is True
        app_with_data.views.create(
            "totals", "SELECT product, SUM(amount) AS t FROM test_db.orders GROUP BY product"
        )
        assert app_with_data.query.sql(sql).from_cache is False

    def test_refreshing_a_view_clears_the_cache(self, app_with_data):
        app_with_data.views.create("v", "SELECT COUNT(*) AS n FROM test_db.users")
        sql = "SELECT * FROM mv_v"
        assert app_with_data.query.sql(sql).from_cache is False
        assert app_with_data.query.sql(sql).from_cache is True
        app_with_data.views.refresh("v")
        assert app_with_data.query.sql(sql).from_cache is False
