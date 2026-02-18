"""Tests for FetchStrategy — smart table identification from SQL and NL questions."""

import pytest

from fusion.catalog import SchemaCatalog
from fusion.strategy import FetchPlan, FetchStrategy, TableTarget


@pytest.fixture
def catalog():
    """Catalog with two sources for federation testing."""
    cat = SchemaCatalog()
    cat.register_source("warp_ecommerce", "warp", {
        "orders": {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "user_id", "type": "integer", "nullable": False},
                {"name": "amount", "type": "double", "nullable": False},
                {"name": "status", "type": "varchar", "nullable": True},
            ],
            "row_count": 10000,
        },
        "customers": {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "name", "type": "varchar", "nullable": False},
                {"name": "email", "type": "varchar", "nullable": True},
            ],
            "row_count": 5000,
        },
        "products": {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "name", "type": "varchar", "nullable": False},
                {"name": "price", "type": "double", "nullable": False},
            ],
            "row_count": 200,
        },
    })
    cat.register_source("warp_analytics", "warp", {
        "events": {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "order_id", "type": "integer", "nullable": False},
                {"name": "event_type", "type": "varchar", "nullable": False},
            ],
            "row_count": 50000,
        },
        "sessions": {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "user_id", "type": "integer", "nullable": False},
                {"name": "duration", "type": "integer", "nullable": True},
            ],
            "row_count": 30000,
        },
    })
    return cat


@pytest.fixture
def strategy(catalog):
    return FetchStrategy(catalog)


# --- TableTarget & FetchPlan ---

class TestTableTarget:
    def test_full_name(self):
        t = TableTarget(source="warp_ecommerce", table="orders")
        assert t.full_name == "warp_ecommerce.orders"

    def test_equality(self):
        t1 = TableTarget(source="src", table="tbl")
        t2 = TableTarget(source="src", table="tbl")
        assert t1 == t2

    def test_inequality(self):
        t1 = TableTarget(source="src1", table="tbl")
        t2 = TableTarget(source="src2", table="tbl")
        assert t1 != t2


class TestFetchPlan:
    def test_add_deduplication(self):
        plan = FetchPlan()
        plan.add("src", "tbl")
        plan.add("src", "tbl")
        assert len(plan.targets) == 1

    def test_is_empty(self):
        plan = FetchPlan()
        assert plan.is_empty()
        plan.add("src", "tbl")
        assert not plan.is_empty()


# --- plan_for_sql ---

class TestPlanForSql:
    def test_qualified_table(self, strategy):
        plan = strategy.plan_for_sql("SELECT * FROM warp_ecommerce.orders")
        assert not plan.is_empty()
        assert plan.targets[0].full_name == "warp_ecommerce.orders"
        assert plan.strategy_used == "sql_parse"

    def test_unqualified_resolves_from_catalog(self, strategy):
        plan = strategy.plan_for_sql("SELECT * FROM orders")
        assert not plan.is_empty()
        assert plan.targets[0].full_name == "warp_ecommerce.orders"

    def test_multi_table_join(self, strategy):
        sql = """
            SELECT o.*, c.name
            FROM warp_ecommerce.orders o
            JOIN warp_ecommerce.customers c ON o.user_id = c.id
        """
        plan = strategy.plan_for_sql(sql)
        names = {t.full_name for t in plan.targets}
        assert "warp_ecommerce.orders" in names
        assert "warp_ecommerce.customers" in names

    def test_cross_source_join(self, strategy):
        sql = """
            SELECT o.*, e.event_type
            FROM warp_ecommerce.orders o
            JOIN warp_analytics.events e ON o.id = e.order_id
        """
        plan = strategy.plan_for_sql(sql)
        sources = {t.source for t in plan.targets}
        assert "warp_ecommerce" in sources
        assert "warp_analytics" in sources

    def test_cte_not_treated_as_table(self, strategy):
        sql = """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY amount DESC) as rn
                FROM warp_ecommerce.orders
            )
            SELECT * FROM ranked WHERE rn <= 10
        """
        plan = strategy.plan_for_sql(sql)
        names = {t.full_name for t in plan.targets}
        # ranked is a CTE, not a real table
        assert "warp_ecommerce.orders" in names
        assert all("ranked" not in t.table for t in plan.targets)

    def test_empty_when_no_catalog_match(self, strategy):
        plan = strategy.plan_for_sql("SELECT * FROM nonexistent_schema.fake_table")
        assert plan.is_empty()

    def test_invalid_sql_returns_empty(self, strategy):
        plan = strategy.plan_for_sql("THIS IS NOT SQL AT ALL")
        # sqlglot may parse this loosely; regardless, no catalog match
        assert plan.is_empty() or all(
            t.full_name in strategy._catalog.list_tables() for t in plan.targets
        )


