"""Tests for FetchPlanner (table resolution + pushdown eligibility)."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer
from fusion.application.planner import FetchPlanner
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.models import ColumnInfo, TableRef, TableSchema


def _schema(*cols, rows=0):
    return TableSchema([ColumnInfo(c, "integer", False) for c in cols], rows)


@pytest.fixture
def catalog():
    cat = SchemaCatalog()
    cat.register_source(
        "warp_ecommerce",
        "warp",
        {
            "orders": _schema("id", "user_id", "amount", rows=10000),
            "customers": _schema("id", "name", rows=5000),
            "products": _schema("id", "price", rows=200),
        },
    )
    cat.register_source(
        "warp_analytics",
        "warp",
        {"events": _schema("id", "order_id", rows=50000), "sessions": _schema("id", rows=30000)},
    )
    return cat


@pytest.fixture
def planner(catalog):
    return FetchPlanner(catalog, SqlglotAnalyzer())


class TestPlanForSql:
    def test_qualified_table(self, planner):
        plan = planner.plan_for_sql("SELECT * FROM warp_ecommerce.orders")
        assert plan.targets == [TableRef("warp_ecommerce", "orders")]
        assert plan.strategy_used == "sql_parse"

    def test_unqualified_resolves_from_catalog(self, planner):
        plan = planner.plan_for_sql("SELECT * FROM orders")
        assert plan.targets == [TableRef("warp_ecommerce", "orders")]

    def test_multi_table_join(self, planner):
        plan = planner.plan_for_sql(
            "SELECT o.*, c.name FROM warp_ecommerce.orders o "
            "JOIN warp_ecommerce.customers c ON o.user_id = c.id"
        )
        assert {t.full_name for t in plan.targets} == {
            "warp_ecommerce.orders",
            "warp_ecommerce.customers",
        }
        assert plan.is_single_source
        assert plan.source_name == "warp_ecommerce"
        assert plan.pushdown_eligible

    def test_cross_source_join(self, planner):
        plan = planner.plan_for_sql(
            "SELECT o.*, e.id FROM warp_ecommerce.orders o "
            "JOIN warp_analytics.events e ON o.id = e.order_id"
        )
        assert {t.source for t in plan.targets} == {"warp_ecommerce", "warp_analytics"}
        assert plan.is_single_source is False
        assert plan.source_name is None
        assert plan.pushdown_eligible is False

    def test_cte_not_treated_as_table(self, planner):
        plan = planner.plan_for_sql(
            "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY amount DESC) rn "
            "FROM warp_ecommerce.orders) SELECT * FROM ranked WHERE rn <= 10"
        )
        assert plan.targets == [TableRef("warp_ecommerce", "orders")]

    def test_empty_when_no_catalog_match(self, planner):
        plan = planner.plan_for_sql("SELECT * FROM nonexistent_schema.fake_table")
        assert plan.is_empty()
        assert plan.is_single_source is False
        assert plan.source_name is None

    def test_invalid_sql_returns_empty_or_known(self, planner, catalog):
        plan = planner.plan_for_sql("THIS IS NOT SQL AT ALL")
        assert plan.is_empty() or all(t in catalog.list_tables() for t in plan.targets)

    def test_single_source_fields(self, planner):
        plan = planner.plan_for_sql("SELECT * FROM warp_ecommerce.orders")
        assert plan.is_single_source is True
        assert plan.has_mv_reference is False
        assert plan.all_targets_unloaded is True
        assert plan.pushdown_eligible is True

    def test_loaded_table_not_eligible(self, planner, catalog):
        catalog.mark_loaded("warp_ecommerce.orders")
        plan = planner.plan_for_sql("SELECT * FROM warp_ecommerce.orders")
        assert plan.all_targets_unloaded is False
        assert plan.pushdown_eligible is False

    def test_mv_reference_disables_pushdown(self, planner):
        plan = planner.plan_for_sql(
            "SELECT * FROM warp_ecommerce.orders o JOIN mv_summary s ON o.id = s.id"
        )
        assert plan.targets == [TableRef("warp_ecommerce", "orders")]
        assert plan.has_mv_reference is True
        assert plan.pushdown_eligible is False

    def test_only_mv_reference_is_empty_plan(self, planner):
        plan = planner.plan_for_sql("SELECT * FROM mv_summary")
        assert plan.is_empty()
        assert plan.has_mv_reference is True
