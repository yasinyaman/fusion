"""Tests for FetchPlanner (table resolution + pushdown eligibility)."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer
from fusion.application.planner import FetchPlanner
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.models import ColumnInfo, TableRef, TableSchema
from fusion.domain.policy import MaterializationPolicy
from fusion.domain.slices import LoadedSlice, Predicate, SliceSpec


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


BIG = TableRef("big", "events")
SMALL = TableRef("big", "users")


@pytest.fixture
def sliced():
    """One huge table and one small one, both in the same source."""
    cat = SchemaCatalog()
    cat.register_source(
        "big",
        "warp",
        {
            "events": TableSchema(
                [ColumnInfo(c, "integer", False) for c in ("id", "user_id", "day", "kind")],
                row_estimate=9_000_000,
            ),
            "users": TableSchema(
                [ColumnInfo(c, "integer", False) for c in ("id", "segment")],
                row_estimate=900,
            ),
        },
    )
    return cat


def _planner(catalog, policy=None, clock=None):
    return FetchPlanner(
        catalog,
        SqlglotAnalyzer(),
        policy or MaterializationPolicy(full_load_max_rows=1000, slice_max_rows=1000),
        clock or (lambda: 0.0),
    )


def _target(plan, ref):
    return next(t for t in plan.targets if t.ref == ref)


class TestPlanQuery:
    def test_small_table_is_loaded_whole(self, sliced):
        plan = _planner(sliced).plan_query("SELECT id FROM big.users WHERE segment = 1")
        target = _target(plan, SMALL)
        assert target.action == "load_full"
        assert target.spec.is_full  # reusable by any later query
        assert target.table_name == "big.users"
        assert not plan.is_refused

    def test_big_table_with_a_filter_becomes_a_slice(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT id FROM big.events WHERE day = 7", estimator=lambda ref, spec: 400
        )
        target = _target(plan, BIG)
        assert target.action == "load_slice"
        assert target.spec.predicates == (Predicate("day", "eq", 7),)
        assert target.spec.columns == frozenset({"id", "day"})
        assert target.estimate == 9_000_000 and target.slice_estimate == 400
        assert target.table_name.startswith("big.events__s_")
        assert plan.table_mapping() == {BIG: target.table_name}

    def test_big_table_without_a_filter_is_refused(self, sliced):
        plan = _planner(sliced).plan_query("SELECT * FROM big.events")
        assert plan.is_refused
        assert "big.events" in plan.refusal and "9,000,000" in plan.refusal
        assert _target(plan, BIG).action == "refuse"

    def test_a_projection_alone_does_not_make_a_big_table_fit(self, sliced):
        # Fewer columns, same number of rows: the budget counts rows.
        plan = _planner(sliced).plan_query(
            "SELECT id FROM big.events", estimator=lambda ref, spec: 900
        )
        target = _target(plan, BIG)
        assert target.action == "refuse" and target.slice_estimate == 9_000_000
        assert plan.is_refused

    def test_a_slice_that_is_still_too_big_is_refused_with_its_size(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT id FROM big.events WHERE day = 7", estimator=lambda ref, spec: 5_000_000
        )
        assert plan.is_refused
        assert "5,000,000" in plan.refusal and "slice_max_rows=1,000" in plan.refusal

    def test_an_unknown_slice_size_is_allowed(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT id FROM big.events WHERE day = 7", estimator=lambda ref, spec: None
        )
        assert _target(plan, BIG).action == "load_slice"

    def test_an_unknown_table_size_is_loaded_whole(self, sliced):
        sliced.register_source("x", "warp", {"t": TableSchema([ColumnInfo("id", "integer")])})
        plan = _planner(sliced).plan_query("SELECT * FROM x.t")
        assert _target(plan, TableRef("x", "t")).action == "load_full"

    def test_a_covering_slice_is_reused(self, sliced):
        spec = SliceSpec(columns=frozenset({"id", "day"}), predicates=(Predicate("day", "eq", 7),))
        loaded = sliced.record_slice(
            LoadedSlice(BIG, spec, spec.table_name_for(BIG), row_count=400)
        )
        plan = _planner(sliced).plan_query("SELECT id FROM big.events WHERE day = 7")
        target = _target(plan, BIG)
        assert target.action == "reuse" and target.covering is loaded
        assert target.table_name == loaded.table_name
        assert not plan.fetches

    def test_a_wider_slice_answers_a_narrower_query(self, sliced):
        wide = SliceSpec(predicates=(Predicate("day", "gt", 5),))
        sliced.record_slice(LoadedSlice(BIG, wide, wide.table_name_for(BIG), row_count=100))
        plan = _planner(sliced).plan_query("SELECT * FROM big.events WHERE day > 9")
        assert _target(plan, BIG).action == "reuse"

    def test_a_narrower_slice_does_not_answer_a_wider_query(self, sliced):
        narrow = SliceSpec(predicates=(Predicate("day", "eq", 7),))
        sliced.record_slice(LoadedSlice(BIG, narrow, narrow.table_name_for(BIG), row_count=10))
        plan = _planner(sliced).plan_query(
            "SELECT * FROM big.events WHERE day > 5", estimator=lambda ref, spec: 10
        )
        assert _target(plan, BIG).action == "load_slice"

    def test_reuse_refreshes_the_last_used_time(self, sliced):
        spec = SliceSpec(predicates=(Predicate("day", "eq", 7),))
        loaded = sliced.record_slice(LoadedSlice(BIG, spec, spec.table_name_for(BIG)))
        _planner(sliced, clock=lambda: 123.0).plan_query("SELECT * FROM big.events WHERE day = 7")
        assert loaded.last_used == 123.0

    def test_complex_sql_falls_back_to_whole_tables(self, sliced):
        plan = _planner(sliced).plan_query("WITH c AS (SELECT id FROM big.users) SELECT * FROM c")
        assert not plan.shape.is_simple_select
        assert _target(plan, SMALL).action == "load_full"

    def test_limit_is_pushed_only_for_a_plain_single_table_read(self, sliced):
        planner = _planner(sliced)
        plain = planner.plan_query(
            "SELECT id FROM big.events LIMIT 5", estimator=lambda ref, spec: 5
        )
        assert _target(plain, BIG).spec.limit == 5
        ordered = planner.plan_query(
            "SELECT id FROM big.events ORDER BY day LIMIT 5", estimator=lambda ref, spec: 5
        )
        assert _target(ordered, BIG).spec.limit is None
        grouped = planner.plan_query(
            "SELECT day, COUNT(*) FROM big.events GROUP BY day LIMIT 5",
            estimator=lambda ref, spec: 5,
        )
        assert _target(grouped, BIG).spec.limit is None

    def test_a_query_touching_nothing_known_plans_nothing(self, sliced):
        plan = _planner(sliced).plan_query("SELECT 1")
        assert plan.targets == () and not plan.is_refused


class TestSemiJoinPlanning:
    def test_a_small_driver_turns_a_refusal_into_key_passing(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT u.id FROM big.users u JOIN big.events e ON u.id = e.user_id WHERE u.segment = 1"
        )
        target = _target(plan, BIG)
        assert target.action == "semi_join"
        assert target.semi_join.driver == SMALL
        assert target.semi_join.driver_table == "big.users"
        assert (target.semi_join.driver_key, target.semi_join.target_key) == ("id", "user_id")
        assert not plan.is_refused

    def test_the_driver_is_planned_before_the_table_it_feeds(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT e.id FROM big.events e JOIN big.users u ON e.user_id = u.id"
        )
        assert [t.ref for t in plan.targets] == [SMALL, BIG]

    def test_no_semi_join_when_the_driver_has_too_many_keys(self, sliced):
        policy = MaterializationPolicy(
            full_load_max_rows=1000, slice_max_rows=1000, semi_join_max_keys=10
        )
        plan = _planner(sliced, policy).plan_query(
            "SELECT e.id FROM big.events e JOIN big.users u ON e.user_id = u.id"
        )
        assert plan.is_refused

    def test_no_semi_join_across_an_outer_join(self, sliced):
        plan = _planner(sliced).plan_query(
            "SELECT e.id FROM big.users u LEFT JOIN big.events e ON u.id = e.user_id"
        )
        assert _target(plan, BIG).action == "refuse"

    def test_no_semi_join_when_the_driver_is_itself_refused(self, sliced):
        sliced.register_source(
            "big2",
            "warp",
            {"other": TableSchema([ColumnInfo("id", "integer")], row_estimate=10**7)},
        )
        plan = _planner(sliced).plan_query(
            "SELECT e.id FROM big2.other o JOIN big.events e ON o.id = e.user_id"
        )
        assert plan.is_refused


class TestBudget:
    def _loaded(self, catalog, name, rows, at):
        spec = SliceSpec(predicates=(Predicate("day", "eq", at),))
        return catalog.record_slice(
            LoadedSlice(
                BIG, spec, f"big.events__s_{name}", row_count=rows, loaded_at=at, last_used=at
            )
        )

    def test_least_recently_used_slices_are_evicted_to_make_room(self, sliced):
        policy = MaterializationPolicy(
            full_load_max_rows=1000, slice_max_rows=10_000, slice_budget_rows=1000
        )
        old = self._loaded(sliced, "old", 600, 1)
        self._loaded(sliced, "new", 300, 9)
        plan = _planner(sliced, policy).plan_query(
            "SELECT id FROM big.events WHERE kind = 3", estimator=lambda ref, spec: 400
        )
        assert plan.evictions == (old.table_name,)
        assert not plan.is_refused

    def test_a_query_that_cannot_fit_even_after_eviction_is_refused(self, sliced):
        policy = MaterializationPolicy(
            full_load_max_rows=1000, slice_max_rows=10_000, slice_budget_rows=500
        )
        self._loaded(sliced, "old", 100, 1)
        plan = _planner(sliced, policy).plan_query(
            "SELECT id FROM big.events WHERE kind = 3", estimator=lambda ref, spec: 900
        )
        assert plan.is_refused and "budget" in plan.refusal
        assert "FUSION_SLICE_BUDGET_ROWS" in plan.refusal

    def test_slices_this_query_needs_are_never_evicted(self, sliced):
        policy = MaterializationPolicy(
            full_load_max_rows=1000, slice_max_rows=10_000, slice_budget_rows=100
        )
        spec = SliceSpec(
            columns=frozenset({"id", "kind"}), predicates=(Predicate("kind", "eq", 3),)
        )
        keep = sliced.record_slice(
            LoadedSlice(BIG, spec, spec.table_name_for(BIG), row_count=500, last_used=1)
        )
        plan = _planner(sliced, policy).plan_query("SELECT id FROM big.events WHERE kind = 3")
        assert keep.table_name not in plan.evictions

    def test_no_eviction_while_inside_the_budget(self, sliced):
        plan = _planner(sliced).plan_query("SELECT id FROM big.users")
        assert plan.evictions == ()
