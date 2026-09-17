"""Tests for ToolService (the 10 LLM tools)."""

import pytest

from fusion.application.tool_schemas import TOOL_NAMES

RESULT_KEYS = {"columns", "rows", "row_count", "truncated", "execution_time_ms", "from_cache"}


@pytest.fixture
def tools(app_with_data):
    return app_with_data.tools


class TestListSources:
    def test_list_sources(self, tools):
        result = tools.list_sources()
        assert len(result["sources"]) == 1
        src = result["sources"][0]
        assert src["source"] == "test_db"
        assert src["type"] == "fake"
        names = {t["name"] for t in src["tables"]}
        assert names == {"test_db.users", "test_db.orders"}

    def test_row_counts_and_loaded_flag(self, tools):
        tables = {t["name"]: t for t in tools.list_sources()["sources"][0]["tables"]}
        assert tables["test_db.users"]["row_count"] == 5
        assert tables["test_db.users"]["columns"] == 3
        assert tables["test_db.users"]["loaded"] is True

    def test_unloaded_table_reports_metadata_count(self, app_lazy):
        tables = {t["name"]: t for t in app_lazy.tools.list_sources()["sources"][0]["tables"]}
        assert tables["warp_main.orders"]["loaded"] is False
        assert tables["warp_main.orders"]["row_count"] == 6


class TestDescribeTable:
    def test_describe_table(self, tools):
        result = tools.describe_table("test_db.users")
        assert result["table"] == "test_db.users"
        assert [c["name"] for c in result["columns"]] == ["id", "name", "segment"]
        assert result["columns"][0] == {"name": "id", "type": "integer", "nullable": False}
        assert result["row_count"] == 5

    def test_describe_table_invalid(self, tools):
        assert "error" in tools.execute("describe_table", {"table": "nonexistent.table"})

    def test_describe_table_bad_identifier(self, tools):
        assert "error" in tools.execute("describe_table", {"table": "DROP TABLE; --"})


class TestQueryData:
    def test_query_data(self, tools):
        result = tools.query_data("SELECT COUNT(*) AS cnt FROM test_db.users")
        assert set(result) == RESULT_KEYS
        assert result["row_count"] == 1
        assert result["rows"][0]["cnt"] == 5
        assert result["truncated"] is False
        assert result["from_cache"] is False

    def test_query_data_with_join(self, tools):
        result = tools.query_data(
            "SELECT u.name, SUM(o.amount) AS total FROM test_db.users u "
            "JOIN test_db.orders o ON u.id = o.user_id GROUP BY u.name ORDER BY total DESC"
        )
        assert result["row_count"] > 0
        assert result["columns"] == ["name", "total"]

    def test_query_data_cached_second_time(self, tools):
        tools.query_data("SELECT 1 AS x")
        assert tools.query_data("SELECT 1 AS x")["from_cache"] is True

    def test_query_data_truncates_at_100(self, tools):
        result = tools.query_data("SELECT * FROM range(250)")
        assert result["row_count"] == 250
        assert len(result["rows"]) == 100
        assert result["truncated"] is True

    def test_query_data_blocks_dml(self, tools):
        assert "error" in tools.execute("query_data", {"sql": "DROP TABLE test_db.users"})
        assert "error" in tools.execute("query_data", {"sql": "DELETE FROM test_db.users"})


class TestSearchData:
    def test_search_exact(self, tools):
        result = tools.search_data("test_db.users", "name", "Alice")
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "Alice"

    def test_search_like(self, tools):
        assert tools.search_data("test_db.users", "segment", "%asic%")["row_count"] == 2

    def test_search_no_results(self, tools):
        assert tools.search_data("test_db.users", "name", "Nonexistent")["row_count"] == 0

    def test_search_with_limit(self, tools):
        assert tools.search_data("test_db.users", "segment", "%", limit=2)["row_count"] <= 2

    def test_search_bad_identifier(self, tools):
        result = tools.execute(
            "search_data",
            {"table": "test_db.users", "filter_column": "1=1; DROP TABLE", "filter_value": "x"},
        )
        assert "error" in result

    def test_search_value_with_quote_is_safe(self, tools):
        result = tools.search_data("test_db.users", "name", "Alice' OR '1'='1")
        assert result["row_count"] == 0

    def test_search_unknown_column_rejected(self, tools):
        result = tools.execute(
            "search_data", {"table": "test_db.users", "filter_column": "ssn", "filter_value": "x"}
        )
        assert "Unknown column" in result["error"]


class TestAggregateData:
    def test_aggregate_sum(self, tools):
        result = tools.aggregate_data("test_db.orders", "product", "amount", "SUM")
        assert result["columns"] == ["product", "sum_amount"]
        assert result["rows"][0] == {"product": "A", "sum_amount": 330.0}

    def test_aggregate_count_and_avg(self, tools):
        assert tools.aggregate_data("test_db.orders", "product", "id", "count")["row_count"] == 3
        assert (
            "avg_amount"
            in tools.aggregate_data("test_db.orders", "product", "amount", "AVG")["columns"]
        )

    def test_aggregate_invalid_func(self, tools):
        result = tools.execute(
            "aggregate_data",
            {
                "table": "test_db.orders",
                "group_by": "product",
                "agg_column": "amount",
                "agg_func": "EVIL",
            },
        )
        assert "Invalid aggregation function" in result["error"]

    def test_aggregate_unknown_column_rejected(self, tools):
        result = tools.execute(
            "aggregate_data",
            {
                "table": "test_db.orders",
                "group_by": "product",
                "agg_column": "nonexistent",
                "agg_func": "SUM",
            },
        )
        assert "Unknown column" in result["error"]


class TestViews:
    def test_create_and_list_views(self, tools):
        result = tools.create_view(
            "test_agg", "SELECT product, SUM(amount) AS total FROM test_db.orders GROUP BY product"
        )
        assert result == {
            "status": "created",
            "name": "test_agg",
            "table_name": "mv_test_agg",
            "refresh": "manual",
        }
        views = tools.list_views()["views"]
        assert len(views) == 1
        assert views[0]["name"] == "test_agg"

    def test_refresh_view(self, tools):
        tools.create_view("test_refresh", "SELECT COUNT(*) AS cnt FROM test_db.users")
        assert tools.refresh_view("test_refresh") == {"status": "refreshed", "name": "test_refresh"}

    def test_refresh_nonexistent_view(self, tools):
        assert "error" in tools.execute("refresh_view", {"name": "nonexistent"})

    def test_create_view_bad_name(self, tools):
        assert "error" in tools.execute(
            "create_view", {"name": "DROP TABLE; --", "sql": "SELECT 1"}
        )

    def test_create_view_on_unloaded_table(self, app_lazy):
        result = app_lazy.tools.create_view("v", "SELECT COUNT(*) AS c FROM warp_main.orders")
        assert result["status"] == "created"
        assert app_lazy.tools.query_data("SELECT * FROM mv_v")["rows"] == [{"c": 6}]


class TestMaterializedViewOperations:
    @pytest.fixture
    def mv_tools(self, tools):
        tools.create_view(
            "test_orders",
            "SELECT product, SUM(amount) AS total, COUNT(*) AS cnt "
            "FROM test_db.orders GROUP BY product",
        )
        return tools

    def test_describe_mv(self, mv_tools):
        result = mv_tools.describe_table("mv_test_orders")
        assert result["table"] == "mv_test_orders"
        assert result["row_count"] == 3
        assert [c["name"] for c in result["columns"]] == ["product", "total", "cnt"]

    def test_describe_mv_not_found(self, tools):
        assert "error" in tools.describe_table("mv_nonexistent")
        assert "error" in tools.execute("describe_table", {"table": "mv_nonexistent"})

    def test_search_mv(self, mv_tools):
        result = mv_tools.search_data("mv_test_orders", "product", "A")
        assert result["rows"][0]["product"] == "A"
        assert mv_tools.search_data("mv_test_orders", "product", "%")["row_count"] == 3

    def test_aggregate_mv(self, mv_tools):
        result = mv_tools.aggregate_data("mv_test_orders", "product", "total", "SUM")
        assert "sum_total" in result["columns"]

    def test_query_data_mv(self, mv_tools):
        result = mv_tools.query_data("SELECT * FROM mv_test_orders ORDER BY total DESC")
        assert result["rows"][0]["product"] == "A"


class TestLoadTable:
    def test_load_table(self, app_lazy, factory):
        result = app_lazy.tools.load_table("warp_main.users")
        assert result == {"status": "loaded", "table": "warp_main.users"}
        assert app_lazy.tools.load_table("warp_main.users") == {
            "status": "already_loaded",
            "table": "warp_main.users",
        }
        assert len(factory.sources["warp_main"].calls_named("fetch_slice")) == 1

    def test_load_table_errors(self, app_lazy):
        assert "source.table" in app_lazy.tools.load_table("users")["error"]
        assert "error" in app_lazy.tools.load_table("warp_main.ghost")
        assert "error" in app_lazy.tools.load_table("ghost.users")
        assert "error" in app_lazy.tools.execute("load_table", {"table": "a;b"})


class TestCacheStats:
    def test_cache_stats(self, tools):
        assert set(tools.cache_stats()) >= {"hits", "misses", "hit_rate"}


class TestDispatch:
    def test_names(self, tools):
        assert tools.names == TOOL_NAMES

    def test_unknown_tool(self, tools):
        assert "Unknown tool" in tools.execute("nonexistent_tool", {})["error"]

    def test_private_method_not_callable(self, tools):
        assert "Unknown tool" in tools.execute("_format_result", {})["error"]
        assert "Unknown tool" in tools.execute("execute", {})["error"]

    def test_missing_arguments(self, tools):
        assert "Invalid arguments" in tools.execute("describe_table", {})["error"]

    def test_none_arguments(self, tools):
        assert "sources" in tools.execute("list_sources", None)


class TestPushdownTools:
    def test_search_pushdown_exact(self, app_lazy, factory):
        result = app_lazy.tools.search_data("warp_main.users", "name", "Alice")
        source = factory.sources["warp_main"]
        assert len(source.calls_named("fetch_filtered")) == 1
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "Alice"
        assert not app_lazy.catalog.is_loaded("warp_main.users")

    def test_search_pushdown_like(self, app_lazy, factory):
        result = app_lazy.tools.search_data("warp_main.users", "segment", "%asic%")
        assert len(factory.sources["warp_main"].calls_named("execute_query")) == 1
        assert result["row_count"] == 2

    def test_search_no_pushdown_when_loaded(self, app_lazy, factory):
        app_lazy.sources.ensure_loaded(["warp_main.users"])
        source = factory.sources["warp_main"]
        source.calls.clear()
        result = app_lazy.tools.search_data("warp_main.users", "name", "Alice")
        assert source.calls_named("fetch_filtered") == []
        assert source.calls_named("execute_query") == []
        assert result["row_count"] == 1

    def test_search_pushdown_fallback_on_error(self, app_lazy, factory):
        source = factory.sources["warp_main"]

        def boom(*args, **kwargs):
            raise RuntimeError("network error")

        source.fetch_filtered = boom
        source.sql_executor = boom
        result = app_lazy.tools.search_data("warp_main.users", "name", "Alice")
        assert result["row_count"] == 1
        assert app_lazy.catalog.is_loaded("warp_main.users")

    def test_aggregate_pushdown(self, app_lazy, factory):
        result = app_lazy.tools.aggregate_data("warp_main.orders", "product", "amount", "SUM")
        assert len(factory.sources["warp_main"].calls_named("execute_query")) == 1
        assert result["rows"][0] == {"product": "A", "sum_amount": 330.0}
        assert result["row_count"] == 3

    def test_aggregate_pushdown_fallback(self, app_lazy, factory):
        source = factory.sources["warp_main"]
        source.sql_executor = lambda sql: (_ for _ in ()).throw(RuntimeError("down"))
        result = app_lazy.tools.aggregate_data("warp_main.orders", "product", "amount", "SUM")
        assert result["rows"][0]["product"] == "A"
        assert app_lazy.catalog.is_loaded("warp_main.orders")
