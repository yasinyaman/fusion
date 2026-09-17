"""Tests for QueryService: pipeline, caching, guardrails, pushdown."""

from dataclasses import replace

import pytest

from fusion.domain.errors import GuardrailViolation, QueryError
from fusion.domain.models import RowSet
from fusion.domain.slices import Predicate
from tests.data import TEST_DB


class TestBasics:
    def test_simple_query(self, app):
        result = app.query.sql("SELECT 1 AS x, 2 AS y")
        assert result.row_count == 1
        assert result.columns == ["x", "y"]
        assert result.rows == [(1, 2)]

    def test_query_with_data(self, app_with_data):
        result = app_with_data.query.sql("SELECT * FROM test_db.users ORDER BY id")
        assert result.row_count == 5
        assert result.to_records()[0]["name"] == "Alice"

    def test_join_query(self, app_with_data):
        result = app_with_data.query.sql(
            "SELECT u.name, SUM(o.amount) AS total FROM test_db.users u "
            "JOIN test_db.orders o ON u.id = o.user_id GROUP BY u.name ORDER BY total DESC"
        )
        assert result.to_records()[0]["name"] == "Alice"

    def test_union_query_allowed(self, app_with_data):
        result = app_with_data.query.sql(
            "SELECT name FROM test_db.users WHERE id = 1 "
            "UNION SELECT product FROM test_db.orders WHERE id = 2"
        )
        assert {r[0] for r in result.rows} == {"Alice", "B"}

    def test_params(self, app_with_data):
        result = app_with_data.query.sql(
            "SELECT name FROM test_db.users WHERE segment = ?", params=["basic"]
        )
        assert result.column_count == 1
        assert sorted(r[0] for r in result.rows) == ["Bob", "Eve"]

    def test_guardrails_block_dml(self, app):
        with pytest.raises(GuardrailViolation):
            app.query.sql("DROP TABLE users")
        with pytest.raises(GuardrailViolation):
            app.query.sql("DELETE FROM users WHERE 1=1")

    def test_read_csv_blocked_by_guardrail(self, app):
        with pytest.raises(GuardrailViolation):
            app.query.sql("SELECT * FROM read_csv('/etc/passwd')")

    def test_query_error_on_bad_sql(self, app):
        with pytest.raises(QueryError):
            app.query.sql("SELECT * FROM nonexistent_table_xyz")


class TestCaching:
    def test_query_caching(self, app_with_data):
        sql = "SELECT COUNT(*) AS cnt FROM test_db.users"
        r1 = app_with_data.query.sql(sql)
        r2 = app_with_data.query.sql(sql)
        assert r1.from_cache is False
        assert r2.from_cache is True
        assert r2.rows == r1.rows
        assert app_with_data.cache.stats()["hits"] == 1

    def test_query_no_cache(self, app_with_data):
        sql = "SELECT COUNT(*) AS cnt FROM test_db.users"
        assert app_with_data.query.sql(sql, use_cache=False).from_cache is False
        assert app_with_data.query.sql(sql, use_cache=False).from_cache is False

    def test_cache_distinguishes_literal_case(self, app_with_data):
        """Regression: 'alice' vs 'ALICE' must not share a cached result."""
        q = app_with_data.query
        lower = q.sql("SELECT name FROM test_db.users WHERE name = 'Alice'")
        upper = q.sql("SELECT name FROM test_db.users WHERE name = 'ALICE'")
        assert lower.rows == [("Alice",)]
        assert upper.rows == []
        assert upper.from_cache is False

    def test_params_are_part_of_cache_key(self, app_with_data):
        sql = "SELECT name FROM test_db.users WHERE name = ?"
        a = app_with_data.query.sql(sql, params=["Alice"])
        b = app_with_data.query.sql(sql, params=["Bob"])
        assert a.rows == [("Alice",)]
        assert b.rows == [("Bob",)]
        assert b.from_cache is False


class TestLazyLoading:
    def test_query_auto_loads_referenced_tables(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": {"t": [{"a": 1}, {"a": 2}]}})
        assert not app.catalog.is_loaded("db.t")
        result = app.query.sql("SELECT SUM(a) AS s FROM db.t")
        assert result.rows == [(3,)]
        assert app.catalog.is_loaded("db.t")

    def test_auto_load_false_fails_for_unloaded(self, app):
        app.sources.connect("db", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        with pytest.raises(QueryError):
            app.query.sql("SELECT * FROM db.t", auto_load=False)


class TestPushdown:
    def test_pushdown_used_for_single_source_unloaded(self, app_lazy, factory):
        result = app_lazy.query.sql("SELECT * FROM warp_main.orders")
        source = factory.sources["warp_main"]
        assert len(source.calls_named("execute_query")) == 1
        assert source.calls_named("fetch_slice") == []
        assert result.row_count == 6
        assert result.columns == ["id", "user_id", "amount", "product"]
        assert not app_lazy.catalog.is_loaded("warp_main.orders")

    def test_pushdown_rewrites_sql(self, app_lazy, factory):
        app_lazy.query.sql("SELECT * FROM warp_main.orders WHERE amount > 100")
        sent = factory.sources["warp_main"].calls_named("execute_query")[0][1]
        assert "warp_main." not in sent
        assert "orders" in sent

    def test_pushdown_result_cached(self, app_lazy, factory):
        r1 = app_lazy.query.sql("SELECT * FROM warp_main.orders")
        r2 = app_lazy.query.sql("SELECT * FROM warp_main.orders")
        assert r1.from_cache is False
        assert r2.from_cache is True
        assert len(factory.sources["warp_main"].calls_named("execute_query")) == 1

    def test_pushdown_fallback_on_failure(self, app_lazy, factory):
        source = factory.sources["warp_main"]

        def boom(sql):
            raise RuntimeError("connection lost")

        source.sql_executor = boom
        result = app_lazy.query.sql("SELECT COUNT(*) AS c FROM warp_main.orders")
        assert result.rows == [(6,)]
        assert app_lazy.catalog.is_loaded("warp_main.orders")
        assert source.calls_named("fetch_slice")

    def test_no_pushdown_when_loaded(self, app_lazy, factory):
        app_lazy.sources.ensure_loaded(["warp_main.orders"])
        source = factory.sources["warp_main"]
        source.calls.clear()
        result = app_lazy.query.sql("SELECT * FROM warp_main.orders")
        assert source.calls_named("execute_query") == []
        assert result.row_count == 6

    def test_no_pushdown_when_auto_load_false(self, app_lazy, factory):
        with pytest.raises(QueryError):
            app_lazy.query.sql("SELECT * FROM warp_main.orders", auto_load=False)
        assert factory.sources["warp_main"].calls_named("execute_query") == []

    def test_no_pushdown_for_source_without_support(self, app, factory):
        app.sources.connect("plain", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        app.query.sql("SELECT * FROM plain.t")
        source = factory.sources["plain"]
        assert source.calls_named("execute_query") == []
        assert len(source.calls_named("fetch_slice")) == 1

    def test_no_pushdown_with_params(self, app_lazy, factory):
        result = app_lazy.query.sql("SELECT * FROM warp_main.users WHERE name = ?", params=["Bob"])
        assert result.row_count == 1
        assert factory.sources["warp_main"].calls_named("execute_query") == []
        assert app_lazy.catalog.is_loaded("warp_main.users")

    def test_no_pushdown_across_sources(self, app, factory):
        app.sources.connect("a", {"type": "fake", "tables": {"t": [{"id": 1}]}, "pushdown": True})
        app.sources.connect("b", {"type": "fake", "tables": {"u": [{"id": 1}]}, "pushdown": True})
        result = app.query.sql("SELECT * FROM a.t JOIN b.u ON a.t.id = b.u.id")
        assert result.row_count == 1
        assert factory.sources["a"].calls_named("execute_query") == []
        assert factory.sources["b"].calls_named("execute_query") == []

    def test_pushdown_result_rows_are_domain_rowset(self, app_lazy, factory):
        factory.sources["warp_main"].sql_executor = lambda sql: RowSet(("x",), [(1,)])
        result = app_lazy.query.sql("SELECT x FROM warp_main.users")
        assert result.columns == ["x"]
        assert result.rows == [(1,)]


@pytest.fixture
def tiny(settings, scheduler, factory):
    """An app that refuses to load more than two rows without a filter."""
    from fusion.bootstrap import build_app

    app = build_app(
        replace(settings, full_load_max_rows=2, slice_max_rows=100),
        scheduler=scheduler,
        source_factory=factory,
    )
    app.sources.connect("db", {"type": "fake", "tables": TEST_DB})
    yield app
    app.close()


class TestSlices:
    """A table too big to load whole is read through the query's own filter."""

    def test_a_filtered_query_loads_only_a_slice(self, tiny, factory):
        result = tiny.query.sql("SELECT * FROM db.users WHERE segment = 'premium'")
        assert result.row_count == 2
        assert not tiny.catalog.is_loaded("db.users")
        slices = tiny.catalog.slices_of("db.users")
        assert len(slices) == 1 and slices[0].row_count == 2
        call = factory.sources["db"].calls_named("fetch_slice")[0]
        assert call[2].predicates == (Predicate("segment", "eq", "premium"),)

    def test_the_original_where_still_filters_after_the_rewrite(self, tiny):
        # The slice only narrows the scan; conditions the source could not
        # take (an OR) must still be applied locally.
        result = tiny.query.sql(
            "SELECT name FROM db.users WHERE segment = 'premium' AND (id = 1 OR id = 99)"
        )
        assert [r["name"] for r in result.to_records()] == ["Alice"]

    def test_a_second_identical_query_does_not_fetch_again(self, tiny, factory):
        for _ in range(2):
            tiny.query.sql("SELECT id FROM db.users WHERE segment = 'premium'", use_cache=False)
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 1

    def test_a_narrower_query_reuses_the_wider_slice(self, tiny, factory):
        tiny.query.sql("SELECT * FROM db.users WHERE id > 1")
        result = tiny.query.sql("SELECT * FROM db.users WHERE id > 3")
        assert result.row_count == 2
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 1

    def test_a_wider_query_fetches_a_new_slice(self, tiny, factory):
        tiny.query.sql("SELECT * FROM db.users WHERE id > 3")
        tiny.query.sql("SELECT * FROM db.users WHERE id > 1")
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 2
        assert len(tiny.catalog.slices_of("db.users")) == 2

    def test_an_unfiltered_query_on_a_big_table_is_refused_with_advice(self, tiny):
        with pytest.raises(QueryError) as error:
            tiny.query.sql("SELECT * FROM db.users")
        message = str(error.value)
        assert "db.users" in message
        assert "add a WHERE condition" in message
        assert "FUSION_FULL_LOAD_MAX_ROWS" in message

    def test_a_refused_query_loads_nothing(self, tiny, factory):
        with pytest.raises(QueryError):
            tiny.query.sql("SELECT * FROM db.users")
        assert tiny.catalog.slices_of("db.users") == []
        assert factory.sources["db"].calls_named("fetch_slice") == []

    def test_the_cache_key_is_the_original_sql(self, tiny):
        sql = "SELECT id FROM db.users WHERE segment = 'premium'"
        assert tiny.query.sql(sql).from_cache is False
        assert tiny.query.sql(sql).from_cache is True

    def test_small_tables_are_still_loaded_whole(self, app_with_data, factory):
        app_with_data.query.sql("SELECT * FROM test_db.orders WHERE amount > 100")
        assert app_with_data.catalog.is_loaded("test_db.orders")
        assert factory.sources["test_db"].calls_named("fetch_slice")[0][2].is_full

    def test_a_join_of_two_slices_still_joins_correctly(self, tiny):
        result = tiny.query.sql(
            "SELECT u.name, o.amount FROM db.users u JOIN db.orders o ON u.id = o.user_id "
            "WHERE u.segment = 'premium' AND o.amount > 100 ORDER BY o.amount"
        )
        assert [r["amount"] for r in result.to_records()] == [150.0, 200.0]
