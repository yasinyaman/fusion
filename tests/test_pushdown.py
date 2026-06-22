"""Tests for query pushdown — routing queries to source database."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from fusion import OLAPEngine
from fusion.catalog import SchemaCatalog
from fusion.connectors.base import BaseConnector
from fusion.connectors.warp import WarpConnector
from fusion.strategy import FetchPlan, FetchStrategy
from fusion.tools.executor import ToolExecutor


# ---------------------------------------------------------------------------
# Connector: supports_pushdown property
# ---------------------------------------------------------------------------

class TestConnectorSupportsPushdown:
    def test_base_connector_default_false(self):
        """BaseConnector.supports_pushdown defaults to False."""

        class DummyConnector(BaseConnector):
            def connect(self): pass
            def fetch_data(self, table): return pd.DataFrame()
            def get_schema(self): return {}
            def close(self): pass

        conn = DummyConnector("dummy", {})
        assert conn.supports_pushdown is False

    def test_warp_connector_supports_pushdown(self):
        """WarpConnector.supports_pushdown returns True."""
        conn = WarpConnector("test", {"type": "warp", "database": "testdb"})
        assert conn.supports_pushdown is True


# ---------------------------------------------------------------------------
# FetchPlan: pushdown eligibility
# ---------------------------------------------------------------------------

class TestFetchPlanPushdownEligibility:
    def test_empty_plan_not_eligible(self):
        plan = FetchPlan()
        assert plan.pushdown_eligible is False

    def test_single_source_unloaded_eligible(self):
        plan = FetchPlan()
        plan.add("warp_main", "orders")
        plan.is_single_source = True
        plan.source_name = "warp_main"
        plan.has_mv_reference = False
        plan.all_targets_unloaded = True
        assert plan.pushdown_eligible is True

    def test_cross_source_not_eligible(self):
        plan = FetchPlan()
        plan.add("warp_main", "orders")
        plan.add("warp_analytics", "events")
        plan.is_single_source = False
        plan.source_name = None
        plan.all_targets_unloaded = True
        assert plan.pushdown_eligible is False

    def test_mv_reference_not_eligible(self):
        plan = FetchPlan()
        plan.add("warp_main", "mv_summary")
        plan.is_single_source = True
        plan.source_name = "warp_main"
        plan.has_mv_reference = True
        plan.all_targets_unloaded = True
        assert plan.pushdown_eligible is False

    def test_loaded_table_not_eligible(self):
        plan = FetchPlan()
        plan.add("warp_main", "orders")
        plan.is_single_source = True
        plan.source_name = "warp_main"
        plan.has_mv_reference = False
        plan.all_targets_unloaded = False  # already in DuckDB
        assert plan.pushdown_eligible is False


# ---------------------------------------------------------------------------
# FetchStrategy: plan_for_sql computes pushdown fields
# ---------------------------------------------------------------------------

class TestStrategyPushdownFields:
    @pytest.fixture
    def catalog(self):
        cat = SchemaCatalog()
        cat.register_source("warp_main", "warp", {
            "orders": {
                "columns": [{"name": "id", "type": "integer", "nullable": False}],
                "row_count": 1000,
            },
            "users": {
                "columns": [{"name": "id", "type": "integer", "nullable": False}],
                "row_count": 500,
            },
        })
        cat.register_source("warp_analytics", "warp", {
            "events": {
                "columns": [{"name": "id", "type": "integer", "nullable": False}],
                "row_count": 5000,
            },
        })
        return cat

    @pytest.fixture
    def strategy(self, catalog):
        return FetchStrategy(catalog)

    def test_single_source_sets_fields(self, strategy):
        plan = strategy.plan_for_sql("SELECT * FROM warp_main.orders")
        assert plan.is_single_source is True
        assert plan.source_name == "warp_main"
        assert plan.has_mv_reference is False
        assert plan.all_targets_unloaded is True
        assert plan.pushdown_eligible is True

    def test_multi_table_same_source(self, strategy):
        plan = strategy.plan_for_sql("""
            SELECT o.*, u.id
            FROM warp_main.orders o
            JOIN warp_main.users u ON o.id = u.id
        """)
        assert plan.is_single_source is True
        assert plan.source_name == "warp_main"
        assert plan.pushdown_eligible is True

    def test_cross_source_not_eligible(self, strategy):
        plan = strategy.plan_for_sql("""
            SELECT o.*, e.id
            FROM warp_main.orders o
            JOIN warp_analytics.events e ON o.id = e.id
        """)
        assert plan.is_single_source is False
        assert plan.source_name is None
        assert plan.pushdown_eligible is False

    def test_loaded_table_not_eligible(self, strategy, catalog):
        catalog.mark_loaded("warp_main.orders")
        plan = strategy.plan_for_sql("SELECT * FROM warp_main.orders")
        assert plan.all_targets_unloaded is False
        assert plan.pushdown_eligible is False

    def test_mv_reference_detected(self, strategy, catalog):
        # Register a materialized view table
        cat_tables = catalog.get_schema("warp_main")["tables"]
        cat_tables["mv_summary"] = {
            "columns": [{"name": "id", "type": "integer", "nullable": False}],
            "row_count": 10,
        }
        plan = strategy.plan_for_sql("SELECT * FROM warp_main.mv_summary")
        assert plan.has_mv_reference is True
        assert plan.pushdown_eligible is False

    def test_empty_plan_defaults(self, strategy):
        plan = strategy.plan_for_sql("SELECT * FROM nonexistent.fake")
        assert plan.is_empty()
        assert plan.is_single_source is False
        assert plan.source_name is None


# ---------------------------------------------------------------------------
# Engine: pushdown routing in sql()
# ---------------------------------------------------------------------------

class TestEnginePushdown:
    @pytest.fixture
    def engine_with_connector(self):
        """Engine with a mock pushdown connector (unloaded tables)."""
        engine = OLAPEngine(database=":memory:", threads=2, memory_limit="512MB")

        # Register metadata (but do NOT load tables)
        engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_main")
        engine.catalog.register_source("warp_main", "warp", {
            "orders": {
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "amount", "type": "double", "nullable": False},
                ],
                "row_count": 1000,
            },
            "users": {
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "name", "type": "varchar", "nullable": False},
                ],
                "row_count": 500,
            },
        })
        # Tables are NOT loaded (no mark_loaded)

        # Create mock connector
        mock_connector = MagicMock()
        mock_connector.supports_pushdown = True
        mock_connector.execute_query.return_value = pd.DataFrame({
            "id": [1, 2, 3],
            "amount": [100.0, 200.0, 300.0],
        })
        engine._connectors["warp_main"] = mock_connector

        yield engine, mock_connector
        engine.close()

    def test_pushdown_used_for_single_source_unloaded(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        result = engine.sql("SELECT * FROM warp_main.orders")

        # Should have called execute_query on the connector
        mock_connector.execute_query.assert_called_once()
        assert result.row_count == 3
        assert result.columns == ["id", "amount"]

    def test_pushdown_rewrites_sql(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        engine.sql("SELECT * FROM warp_main.orders WHERE amount > 100")

        # The SQL sent to connector should not have the source prefix
        call_args = mock_connector.execute_query.call_args
        sent_sql = call_args[0][0]
        assert "warp_main." not in sent_sql
        assert "orders" in sent_sql

    def test_pushdown_result_cached(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        r1 = engine.sql("SELECT * FROM warp_main.orders")
        assert r1.from_cache is False

        r2 = engine.sql("SELECT * FROM warp_main.orders")
        assert r2.from_cache is True

        # execute_query should only be called once (second was from cache)
        assert mock_connector.execute_query.call_count == 1

    def test_pushdown_fallback_on_failure(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        # Make pushdown fail
        mock_connector.execute_query.side_effect = Exception("connection lost")

        # Should also set fetch_data to return something for the fallback
        mock_connector.fetch_data.return_value = pd.DataFrame({
            "id": [1, 2], "amount": [10.0, 20.0],
        })

        result = engine.sql("SELECT * FROM warp_main.orders")
        # Fallback should have loaded the table and run on DuckDB
        assert result.row_count == 2

    def test_no_pushdown_when_loaded(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        # Load the table first
        mock_connector.fetch_data.return_value = pd.DataFrame({
            "id": [1, 2], "amount": [10.0, 20.0],
        })
        engine.ensure_tables_loaded(["warp_main.orders"])

        # Reset mock to track new calls
        mock_connector.execute_query.reset_mock()

        result = engine.sql("SELECT * FROM warp_main.orders")
        # Should NOT call execute_query (pushdown) since table is loaded
        mock_connector.execute_query.assert_not_called()
        assert result.row_count == 2

    def test_no_pushdown_when_auto_load_false(self, engine_with_connector):
        engine, mock_connector = engine_with_connector

        # With auto_load=False, pushdown should not be attempted
        # But the query will fail since table isn't loaded
        with pytest.raises(Exception):
            engine.sql("SELECT * FROM warp_main.orders", auto_load=False)

        mock_connector.execute_query.assert_not_called()

    def test_no_pushdown_for_connector_without_support(self, engine_with_connector):
        engine, mock_connector = engine_with_connector
        mock_connector.supports_pushdown = False

        # Should fallback to loading the table
        mock_connector.fetch_data.return_value = pd.DataFrame({
            "id": [1], "amount": [10.0],
        })

        engine.sql("SELECT * FROM warp_main.orders")
        mock_connector.execute_query.assert_not_called()
        mock_connector.fetch_data.assert_called_once()


class TestRewriteSqlForPushdown:
    def test_removes_source_prefix(self):
        plan = FetchPlan()
        plan.add("warp_main", "orders")
        plan.source_name = "warp_main"

        result = OLAPEngine._rewrite_sql_for_pushdown(
            "SELECT * FROM warp_main.orders WHERE amount > 100", plan
        )
        assert "warp_main." not in result
        assert "orders" in result
        assert "amount > 100" in result

    def test_removes_prefix_in_join(self):
        plan = FetchPlan()
        plan.add("warp_main", "orders")
        plan.add("warp_main", "users")
        plan.source_name = "warp_main"

        result = OLAPEngine._rewrite_sql_for_pushdown(
            "SELECT * FROM warp_main.orders o JOIN warp_main.users u ON o.id = u.id",
            plan,
        )
        assert "warp_main." not in result
        assert "orders" in result
        assert "users" in result


# ---------------------------------------------------------------------------
# Executor: tool-level pushdown (search_data, aggregate_data)
# ---------------------------------------------------------------------------

class TestExecutorSearchPushdown:
    @pytest.fixture
    def executor_with_connector(self):
        engine = OLAPEngine(database=":memory:", threads=2, memory_limit="512MB")

        engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_main")
        engine.catalog.register_source("warp_main", "warp", {
            "users": {
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "name", "type": "varchar", "nullable": False},
                    {"name": "segment", "type": "varchar", "nullable": True},
                ],
                "row_count": 100,
            },
        })
        # NOT loaded

        mock_connector = MagicMock()
        mock_connector.supports_pushdown = True
        mock_connector.fetch_data_filtered.return_value = pd.DataFrame({
            "id": [1], "name": ["Alice"], "segment": ["premium"],
        })
        mock_connector.execute_query.return_value = pd.DataFrame({
            "id": [1, 2], "name": ["Alice", "Bob"], "segment": ["premium", "basic"],
        })
        engine._connectors["warp_main"] = mock_connector

        executor = ToolExecutor(engine)
        yield executor, mock_connector
        engine.close()

    def test_search_pushdown_exact(self, executor_with_connector):
        executor, mock_conn = executor_with_connector
        result = executor.search_data("warp_main.users", "name", "Alice")

        # Should use fetch_data_filtered for exact match
        mock_conn.fetch_data_filtered.assert_called_once()
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "Alice"

    def test_search_pushdown_like(self, executor_with_connector):
        executor, mock_conn = executor_with_connector
        result = executor.search_data("warp_main.users", "segment", "%asic%")

        # Should use execute_query for LIKE pattern
        mock_conn.execute_query.assert_called_once()
        assert result["row_count"] == 2

    def test_search_no_pushdown_when_loaded(self, executor_with_connector):
        executor, mock_conn = executor_with_connector

        # Load the table first
        mock_conn.fetch_data.return_value = pd.DataFrame({
            "id": [1, 2],
            "name": ["Alice", "Bob"],
            "segment": ["premium", "basic"],
        })
        executor._engine.ensure_tables_loaded(["warp_main.users"])
        mock_conn.reset_mock()

        # Should use DuckDB path, not pushdown
        result = executor.search_data("warp_main.users", "name", "Alice")
        mock_conn.fetch_data_filtered.assert_not_called()
        mock_conn.execute_query.assert_not_called()
        assert result["row_count"] == 1

    def test_search_pushdown_fallback_on_error(self, executor_with_connector):
        executor, mock_conn = executor_with_connector

        # Make ALL pushdown methods fail (tool-level and engine-level)
        mock_conn.fetch_data_filtered.side_effect = Exception("network error")
        mock_conn.execute_query.side_effect = Exception("network error")

        # Should fallback to DuckDB (which will load the table via fetch_data)
        mock_conn.fetch_data.return_value = pd.DataFrame({
            "id": [1], "name": ["Alice"], "segment": ["premium"],
        })

        result = executor.search_data("warp_main.users", "name", "Alice")
        assert result["row_count"] == 1


class TestExecutorAggregatePushdown:
    @pytest.fixture
    def executor_with_connector(self):
        engine = OLAPEngine(database=":memory:", threads=2, memory_limit="512MB")

        engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_main")
        engine.catalog.register_source("warp_main", "warp", {
            "orders": {
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "product", "type": "varchar", "nullable": False},
                    {"name": "amount", "type": "double", "nullable": False},
                ],
                "row_count": 1000,
            },
        })
        # NOT loaded

        mock_connector = MagicMock()
        mock_connector.supports_pushdown = True
        mock_connector.execute_query.return_value = pd.DataFrame({
            "product": ["A", "B", "C"],
            "sum_amount": [1000.0, 500.0, 250.0],
        })
        engine._connectors["warp_main"] = mock_connector

        executor = ToolExecutor(engine)
        yield executor, mock_connector
        engine.close()

    def test_aggregate_pushdown(self, executor_with_connector):
        executor, mock_conn = executor_with_connector
        result = executor.aggregate_data(
            "warp_main.orders", "product", "amount", "SUM"
        )

        mock_conn.execute_query.assert_called_once()
        assert result["row_count"] == 3
        assert result["rows"][0]["product"] == "A"

    def test_aggregate_no_pushdown_when_loaded(self, executor_with_connector):
        executor, mock_conn = executor_with_connector

        # Load the table
        mock_conn.fetch_data.return_value = pd.DataFrame({
            "id": [1, 2, 3],
            "product": ["A", "A", "B"],
            "amount": [100.0, 200.0, 50.0],
        })
        executor._engine.ensure_tables_loaded(["warp_main.orders"])
        mock_conn.reset_mock()

        result = executor.aggregate_data(
            "warp_main.orders", "product", "amount", "SUM"
        )
        mock_conn.execute_query.assert_not_called()
        assert result["row_count"] == 2

    def test_aggregate_pushdown_fallback_on_error(self, executor_with_connector):
        executor, mock_conn = executor_with_connector

        mock_conn.execute_query.side_effect = Exception("query failed")

        # Fallback: DuckDB loads the table
        mock_conn.fetch_data.return_value = pd.DataFrame({
            "id": [1, 2],
            "product": ["A", "B"],
            "amount": [100.0, 50.0],
        })

        result = executor.aggregate_data(
            "warp_main.orders", "product", "amount", "SUM"
        )
        assert result["row_count"] == 2


class TestFormatDataframeResult:
    def test_basic(self):
        df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        result = ToolExecutor._format_dataframe_result(df)
        assert result["columns"] == ["a", "b"]
        assert result["row_count"] == 2
        assert result["rows"][0]["a"] == 1

    def test_empty(self):
        df = pd.DataFrame()
        result = ToolExecutor._format_dataframe_result(df)
        assert result["columns"] == []
        assert result["rows"] == []
        assert result["row_count"] == 0

    def test_truncation(self):
        df = pd.DataFrame({"x": range(150)})
        result = ToolExecutor._format_dataframe_result(df)
        assert result["truncated"] is True
        assert len(result["rows"]) == 100
        assert result["row_count"] == 150
