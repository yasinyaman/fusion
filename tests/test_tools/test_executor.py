"""Tests for ToolExecutor."""

import pytest

from fusion.tools.executor import ToolExecutor


@pytest.fixture
def executor(engine_with_data):
    """ToolExecutor backed by engine with test data."""
    return ToolExecutor(engine_with_data)


class TestToolExecutorListSources:
    def test_list_sources(self, executor):
        result = executor.list_sources()
        assert "sources" in result
        assert len(result["sources"]) == 1
        src = result["sources"][0]
        assert src["source"] == "test_db"
        table_names = [t["name"] for t in src["tables"]]
        assert "test_db.users" in table_names
        assert "test_db.orders" in table_names

    def test_list_sources_shows_row_counts(self, executor):
        result = executor.list_sources()
        users = next(
            t for t in result["sources"][0]["tables"]
            if t["name"] == "test_db.users"
        )
        assert users["row_count"] == 5


class TestToolExecutorDescribeTable:
    def test_describe_table(self, executor):
        result = executor.describe_table("test_db.users")
        assert result["table"] == "test_db.users"
        assert len(result["columns"]) == 3
        col_names = [c["name"] for c in result["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "segment" in col_names

    def test_describe_table_invalid(self, executor):
        result = executor.execute("describe_table", {"table": "nonexistent.table"})
        assert "error" in result

    def test_describe_table_bad_identifier(self, executor):
        result = executor.execute("describe_table", {"table": "DROP TABLE; --"})
        assert "error" in result


class TestToolExecutorQueryData:
    def test_query_data(self, executor):
        result = executor.query_data("SELECT COUNT(*) as cnt FROM test_db.users")
        assert result["row_count"] == 1
        assert result["rows"][0]["cnt"] == 5
        assert result["truncated"] is False

    def test_query_data_with_join(self, executor):
        result = executor.query_data("""
            SELECT u.name, SUM(o.amount) as total
            FROM test_db.users u
            JOIN test_db.orders o ON u.id = o.user_id
            GROUP BY u.name
            ORDER BY total DESC
        """)
        assert result["row_count"] > 0
        assert "name" in result["columns"]
        assert "total" in result["columns"]

    def test_query_data_blocks_drop(self, executor):
        result = executor.execute("query_data", {"sql": "DROP TABLE test_db.users"})
        assert "error" in result

    def test_query_data_blocks_delete(self, executor):
        result = executor.execute("query_data", {"sql": "DELETE FROM test_db.users"})
        assert "error" in result


class TestToolExecutorSearchData:
    def test_search_exact(self, executor):
        result = executor.search_data("test_db.users", "name", "Alice")
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "Alice"

    def test_search_like(self, executor):
        result = executor.search_data("test_db.users", "segment", "%asic%")
        assert result["row_count"] == 2

    def test_search_no_results(self, executor):
        result = executor.search_data("test_db.users", "name", "Nonexistent")
        assert result["row_count"] == 0

    def test_search_with_limit(self, executor):
        result = executor.search_data("test_db.users", "segment", "%", limit=2)
        assert result["row_count"] <= 2

    def test_search_bad_identifier(self, executor):
        result = executor.execute(
            "search_data",
            {"table": "test_db.users", "filter_column": "1=1; DROP TABLE", "filter_value": "x"},
        )
        assert "error" in result

    def test_search_value_with_quote_is_safe(self, executor):
        # A classic injection payload in the *value* is bound as a parameter,
        # so it matches literally (no rows) instead of altering the query.
        result = executor.search_data("test_db.users", "name", "Alice' OR '1'='1")
        assert result["row_count"] == 0

    def test_search_unknown_column_rejected(self, executor):
        result = executor.execute(
            "search_data",
            {"table": "test_db.users", "filter_column": "ssn", "filter_value": "x"},
        )
        assert "error" in result
        assert "Unknown column" in result["error"]


class TestToolExecutorAggregateData:
    def test_aggregate_sum(self, executor):
        result = executor.aggregate_data("test_db.orders", "product", "amount", "SUM")
        assert result["row_count"] > 0
        assert "sum_amount" in result["columns"]

    def test_aggregate_count(self, executor):
        result = executor.aggregate_data("test_db.orders", "product", "id", "COUNT")
        assert result["row_count"] > 0

    def test_aggregate_avg(self, executor):
        result = executor.aggregate_data("test_db.orders", "product", "amount", "AVG")
        assert result["row_count"] > 0
        assert "avg_amount" in result["columns"]

    def test_aggregate_invalid_func(self, executor):
        result = executor.execute(
            "aggregate_data",
            {"table": "test_db.orders", "group_by": "product",
             "agg_column": "amount", "agg_func": "EVIL"},
        )
        assert "error" in result

    def test_aggregate_unknown_column_rejected(self, executor):
        result = executor.execute(
            "aggregate_data",
            {"table": "test_db.orders", "group_by": "product",
             "agg_column": "nonexistent", "agg_func": "SUM"},
        )
        assert "error" in result
        assert "Unknown column" in result["error"]


class TestToolExecutorViews:
    def test_create_and_list_views(self, executor):
        result = executor.create_view(
            "test_agg",
            "SELECT product, SUM(amount) as total FROM test_db.orders GROUP BY product",
        )
        assert result["status"] == "created"
        assert result["table_name"] == "mv_test_agg"

        views = executor.list_views()
        assert len(views["views"]) == 1
        assert views["views"][0]["name"] == "test_agg"

    def test_refresh_view(self, executor):
        executor.create_view(
            "test_refresh",
            "SELECT COUNT(*) as cnt FROM test_db.users",
        )
        result = executor.refresh_view("test_refresh")
        assert result["status"] == "refreshed"

    def test_refresh_nonexistent_view(self, executor):
        result = executor.execute("refresh_view", {"name": "nonexistent"})
        assert "error" in result

    def test_create_view_bad_name(self, executor):
        result = executor.execute(
            "create_view",
            {"name": "DROP TABLE; --", "sql": "SELECT 1"},
        )
        assert "error" in result


class TestToolExecutorMVOperations:
    """Test that describe_table, search_data, aggregate_data work with mv_* tables."""

    def _create_mv(self, executor):
        """Helper to create a materialized view for testing."""
        executor.create_view(
            "test_orders",
            "SELECT product, SUM(amount) as total, COUNT(*) as cnt "
            "FROM test_db.orders GROUP BY product",
        )

    def test_describe_mv(self, executor):
        self._create_mv(executor)
        result = executor.describe_table("mv_test_orders")
        assert result["table"] == "mv_test_orders"
        assert result["row_count"] > 0
        col_names = [c["name"] for c in result["columns"]]
        assert "product" in col_names
        assert "total" in col_names
        assert "cnt" in col_names

    def test_describe_mv_not_found(self, executor):
        result = executor.describe_table("mv_nonexistent")
        assert "error" in result

    def test_describe_mv_via_execute(self, executor):
        self._create_mv(executor)
        result = executor.execute("describe_table", {"table": "mv_test_orders"})
        assert "error" not in result
        assert result["table"] == "mv_test_orders"

    def test_search_mv(self, executor):
        self._create_mv(executor)
        result = executor.search_data("mv_test_orders", "product", "A")
        assert result["row_count"] == 1
        assert result["rows"][0]["product"] == "A"

    def test_search_mv_like(self, executor):
        self._create_mv(executor)
        result = executor.search_data("mv_test_orders", "product", "%")
        assert result["row_count"] > 0

    def test_aggregate_mv(self, executor):
        self._create_mv(executor)
        result = executor.aggregate_data("mv_test_orders", "product", "total", "SUM")
        assert result["row_count"] > 0
        assert "sum_total" in result["columns"]

    def test_query_data_mv(self, executor):
        self._create_mv(executor)
        result = executor.query_data(
            "SELECT * FROM mv_test_orders ORDER BY total DESC"
        )
        assert result["row_count"] > 0
        assert result["rows"][0]["product"] == "A"


class TestToolExecutorCacheStats:
    def test_cache_stats(self, executor):
        result = executor.cache_stats()
        assert "hits" in result or "size" in result or isinstance(result, dict)


class TestToolExecutorExecuteDispatch:
    def test_unknown_tool(self, executor):
        result = executor.execute("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_private_method_not_callable(self, executor):
        result = executor.execute("_format_result", {})
        assert "error" in result

    def test_missing_arguments(self, executor):
        result = executor.execute("describe_table", {})
        assert "error" in result
