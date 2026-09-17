"""Tests for SourceService."""

import json
from dataclasses import replace

import pytest

from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.models import ColumnInfo, RefreshSpec, RowSet, TableRef, TableSchema
from fusion.domain.policy import TargetPlan
from fusion.domain.slices import Predicate, SliceSpec
from tests.data import ORDERS, TEST_DB, USERS


class TestConnect:
    def test_connect_registers_metadata_only(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        assert app.catalog.source_names() == ["db"]
        assert app.catalog.get_table("db.users").column_names == {"id", "name", "segment"}
        assert app.catalog.get_table("db.users").row_count == 5
        assert not app.catalog.is_loaded("db.users")
        assert factory.sources["db"].calls_named("fetch_slice") == []
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
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 2

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
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 1

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
            assert factory.sources["db"].calls_named("fetch_slice") == [
                ("fetch_slice", "users", SliceSpec.FULL, 2)
            ]
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
        assert source.calls_named("fetch_slice") == [("fetch_slice", "users", SliceSpec.FULL, None)]

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


class TestSlices:
    """Slice loading: staging, publishing, completeness and eviction."""

    @pytest.fixture
    def sliced(self, app):
        app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
        return app

    def _plan(self, spec=None, action="load_slice"):
        return TargetPlan(
            TableRef("db", "users"),
            spec or SliceSpec(predicates=(Predicate("segment", "eq", "premium"),)),
            action,
        )

    def test_ensure_slices_materializes_and_maps(self, sliced):
        plan = self._plan()
        mapping = sliced.sources.ensure_slices([plan])
        assert mapping == {TableRef("db", "users"): plan.table_name}
        assert sliced.store.count(plan.table_name) == 2
        assert not sliced.catalog.is_loaded("db.users")

    def test_a_slice_is_registered_with_its_size(self, sliced):
        plan = self._plan()
        sliced.sources.ensure_slices([plan])
        loaded = sliced.catalog.slices_of("db.users")[0]
        assert loaded.row_count == 2 and loaded.complete
        assert loaded.spec == plan.spec

    def test_a_projected_slice_keeps_its_columns(self, sliced):
        plan = self._plan(SliceSpec(columns=frozenset({"id", "name"})))
        sliced.sources.ensure_slices([plan])
        assert {c.name for c in sliced.store.describe(plan.table_name)} == {"id", "name"}

    def test_an_empty_slice_still_has_the_right_columns(self, sliced):
        plan = self._plan(SliceSpec(predicates=(Predicate("segment", "eq", "nope"),)))
        sliced.sources.ensure_slices([plan])
        assert sliced.store.count(plan.table_name) == 0
        assert {c.name for c in sliced.store.describe(plan.table_name)} == {
            "id",
            "name",
            "segment",
        }

    def test_a_full_load_updates_the_catalog_row_count(self, sliced):
        sliced.sources.ensure_slices([self._plan(SliceSpec.FULL, "load_full")])
        assert sliced.catalog.is_loaded("db.users")
        assert sliced.catalog.get_table("db.users").row_count == 5

    def test_reuse_does_not_fetch_again(self, sliced, factory):
        plan = self._plan()
        sliced.sources.ensure_slices([plan])
        loaded = sliced.catalog.slices_of("db.users")[0]
        reuse = TargetPlan(TableRef("db", "users"), plan.spec, "reuse", covering=loaded)
        assert sliced.sources.ensure_slices([reuse]) == {TableRef("db", "users"): plan.table_name}
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 1

    def test_a_refused_target_raises_with_its_reason(self, sliced):
        refused = TargetPlan(TableRef("db", "users"), SliceSpec.FULL, "refuse", reason="too big")
        with pytest.raises(QueryError, match="too big"):
            sliced.sources.ensure_slices([refused])

    def test_the_staging_table_never_survives(self, sliced):
        plan = self._plan()
        sliced.sources.ensure_slices([plan])
        with pytest.raises(QueryError):
            sliced.store.count(f"{plan.table_name}__tmp")

    def test_a_failed_load_leaves_no_staging_table(self, sliced, factory):
        def boom(*args, **kwargs):
            raise QueryError("source exploded")

        factory.sources["db"].fetch_slice = boom
        plan = self._plan()
        with pytest.raises(QueryError, match="exploded"):
            sliced.sources.ensure_slices([plan])
        with pytest.raises(QueryError):
            sliced.store.count(f"{plan.table_name}__tmp")

    def test_a_reload_replaces_the_slice_atomically(self, sliced, factory):
        plan = self._plan(SliceSpec.FULL, "load_full")
        sliced.sources.ensure_slices([plan])
        factory.sources["db"].set_table("users", [{"id": 9, "name": "Zoe", "segment": "basic"}])
        sliced.sources.ensure_slices([plan])
        assert sliced.store.execute("SELECT name FROM db.users").rows == [("Zoe",)]

    def test_evict_slice_drops_the_table_and_forgets_it(self, sliced):
        plan = self._plan()
        sliced.sources.ensure_slices([plan])
        sliced.sources.evict_slice(plan.table_name)
        assert sliced.catalog.slices_of("db.users") == []
        with pytest.raises(QueryError):
            sliced.store.count(plan.table_name)

    def test_unload_removes_every_slice_of_a_table(self, sliced):
        sliced.sources.ensure_slices([self._plan()])
        sliced.sources.ensure_slices([self._plan(SliceSpec.FULL, "load_full")])
        sliced.sources.unload("db.users")
        assert sliced.catalog.slices_of("db.users") == []
        assert not sliced.catalog.is_loaded("db.users")

    def test_estimate_slice_asks_the_source(self, sliced):
        spec = SliceSpec(predicates=(Predicate("segment", "eq", "premium"),))
        assert sliced.sources.estimate_slice(TableRef("db", "users"), spec) == 2
        assert sliced.sources.estimate_slice(TableRef("nope", "users"), spec) is None

    def test_estimate_slice_survives_a_failing_source(self, sliced, factory):
        factory.sources["db"].estimate_slice = lambda *a, **k: 1 / 0
        assert sliced.sources.estimate_slice(TableRef("db", "users"), SliceSpec.FULL) is None

    def test_a_source_capped_read_is_marked_incomplete(self, sliced, factory):
        class Capped:
            row_limit = 2

            columns = ("id", "name", "segment")
            schema = None

            def arrow_reader(self):
                return None

            def __iter__(self):
                yield RowSet.from_records(
                    [
                        {"id": 1, "name": "Alice", "segment": "premium"},
                        {"id": 2, "name": "Bob", "segment": "basic"},
                    ]
                )

            def close(self):
                return None

        factory.sources["db"].fetch_slice = lambda *a, **k: Capped()
        sliced.sources.ensure_slices([self._plan(SliceSpec.FULL, "load_full")])
        loaded = sliced.catalog.slices_of("db.users")[0]
        assert loaded.complete is False
        assert not sliced.catalog.is_loaded("db.users")  # never answers a narrower query


ORDERS_V2 = [
    {"id": 1, "user_id": 1, "amount": 100.0, "product": "A", "updated_at": 10},
    {"id": 2, "user_id": 2, "amount": 50.0, "product": "B", "updated_at": 20},
]


class TestIncrementalRefresh:
    """A watermarked table is topped up instead of re-fetched."""

    @pytest.fixture
    def app(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        a = build_app(
            replace(
                settings,
                refresh_config=json.dumps(
                    {"db.orders": {"watermark_column": "updated_at", "key_columns": ["id"]}}
                ),
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        a.sources.connect("db", {"type": "fake", "tables": {"orders": ORDERS_V2}})
        a.sources.ensure_loaded(["db.orders"])
        yield a
        a.close()

    def _rows(self, app):
        return app.store.execute("SELECT id, amount FROM db.orders ORDER BY id").rows

    def test_only_rows_above_the_watermark_are_fetched(self, app, factory):
        factory.sources["db"].set_table(
            "orders", [*ORDERS_V2, {"id": 3, "user_id": 3, "amount": 7.0, "updated_at": 30}]
        )
        app.sources.refresh_all()
        assert self._rows(app) == [(1, 100.0), (2, 50.0), (3, 7.0)]
        fetches = [c for c in factory.sources["db"].calls_named("fetch_slice") if c[1] == "orders"]
        assert fetches[-1][2].predicates == (Predicate("updated_at", "gt", 20),)

    def test_an_updated_row_replaces_its_old_copy(self, app, factory):
        factory.sources["db"].set_table(
            "orders",
            [
                {"id": 1, "user_id": 1, "amount": 999.0, "product": "A", "updated_at": 40},
                *ORDERS_V2[1:],
            ],
        )
        app.sources.refresh_all()
        assert self._rows(app) == [(1, 999.0), (2, 50.0)]
        assert app.catalog.get_table("db.orders").row_count == 2

    def test_without_key_columns_new_rows_are_appended(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(
                settings,
                refresh_config=json.dumps({"db.orders": {"watermark_column": "updated_at"}}),
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": {"orders": ORDERS_V2}})
            app.sources.ensure_loaded(["db.orders"])
            factory.sources["db"].set_table(
                "orders", [*ORDERS_V2, {"id": 3, "user_id": 3, "amount": 7.0, "updated_at": 30}]
            )
            app.sources.refresh_all()
            assert app.store.count("db.orders") == 3
        finally:
            app.close()

    def test_nothing_new_means_nothing_changes(self, app, factory):
        app.sources.refresh_all()
        assert self._rows(app) == [(1, 100.0), (2, 50.0)]

    def test_a_table_without_a_watermark_is_refetched_whole(self, app, factory):
        app.sources.connect("other", {"type": "fake", "tables": {"users": USERS}})
        app.sources.ensure_loaded(["other.users"])
        factory.sources["other"].set_table("users", [{"id": 1, "name": "Solo", "segment": "x"}])
        app.sources.refresh_all()
        assert app.store.count("other.users") == 1
        specs = [c[2] for c in factory.sources["other"].calls_named("fetch_slice")]
        assert specs[-1].is_full

    def test_partial_slices_are_dropped_rather_than_refreshed(self, app):
        target = TargetPlan(
            TableRef("db", "orders"),
            SliceSpec(predicates=(Predicate("amount", "gt", 60),)),
            "load_slice",
        )
        table = app.sources.ensure_slices([target])[TableRef("db", "orders")]
        assert app.store.count(table) == 1
        app.sources.refresh_all()
        remaining = app.catalog.slices_of("db.orders")
        assert [s.table_name for s in remaining] == ["db.orders"]
        assert remaining[0].is_full
        with pytest.raises(QueryError):
            app.store.count(table)

    def test_a_refresh_clears_the_cache(self, app, factory):
        sql = "SELECT COUNT(*) AS n FROM db.orders"
        assert app.query.sql(sql).rows == [(2,)]
        assert app.query.sql(sql).from_cache is True
        factory.sources["db"].set_table(
            "orders", [*ORDERS_V2, {"id": 3, "user_id": 3, "amount": 7.0, "updated_at": 30}]
        )
        app.sources.refresh_all()
        result = app.query.sql(sql)
        assert result.from_cache is False and result.rows == [(3,)]

    def test_a_refresh_that_changes_nothing_leaves_the_cache_alone(self, app):
        app.sources.connect("empty", {"type": "fake", "tables": {}})
        app.query.sql("SELECT 1 AS x")
        app.cache.clear()
        app.query.sql("SELECT 1 AS x")
        assert app.query.sql("SELECT 1 AS x").from_cache is True

    def test_an_invalid_watermark_column_is_refused(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(
                settings,
                refresh_config=json.dumps({"db.orders": {"watermark_column": "a; DROP TABLE t"}}),
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": {"orders": ORDERS_V2}})
            app.sources.ensure_loaded(["db.orders"])
            with pytest.raises(QueryError, match="watermark"):
                app.sources.refresh_all(force=True)
        finally:
            app.close()

    def test_source_config_can_carry_the_refresh_spec(self, app, factory):
        app.sources.connect(
            "cfg",
            {
                "type": "fake",
                "tables": {"orders": ORDERS_V2},
                "refresh": {"orders": {"watermark_column": "updated_at", "key_columns": ["id"]}},
            },
        )
        assert app.sources.refresh_spec("cfg.orders") == RefreshSpec("updated_at", ("id",))
        assert app.sources.refresh_spec("cfg.nope") is None


class TestStreamTypesWin:
    """A source that announces its column types beats the catalog's guess."""

    def test_the_streams_own_schema_is_used(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": {"t": [{"n": "1"}]}})
        # The catalog inferred varchar from a sample; the stream says integer.
        assert app.catalog.get_table("db.t").columns[0].type == "varchar"

        class Typed:
            row_limit = None
            columns = ("n",)
            schema = TableSchema([ColumnInfo("n", "integer", False)])

            def arrow_reader(self):
                return None

            def __iter__(self):
                yield RowSet.from_records([{"n": 1}, {"n": 2}])

            def close(self):
                return None

        factory.sources["db"].fetch_slice = lambda *a, **k: Typed()
        app.sources.ensure_loaded(["db.t"])
        assert [c.type for c in app.store.describe("db.t")] == ["BIGINT"]
        assert app.store.execute("SELECT SUM(n) FROM db.t").rows == [(3,)]
