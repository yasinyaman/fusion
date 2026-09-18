"""Materialization budgets, target plans and the refusal messages."""

from fusion.domain.models import TableRef
from fusion.domain.policy import (
    MaterializationPolicy,
    SemiJoinSpec,
    TargetPlan,
    budget_message,
    refusal_message,
)
from fusion.domain.slices import LoadedSlice, Predicate, SliceSpec

REF = TableRef("db", "events")


class TestPolicy:
    def test_defaults(self):
        policy = MaterializationPolicy()
        assert policy.full_load_max_rows == 500_000
        assert policy.slice_max_rows == 500_000
        assert policy.slice_budget_rows == 2_000_000
        assert policy.semi_join_max_keys == 50_000
        assert policy.in_chunk_size == 1_000

    def test_unknown_estimates_are_allowed(self):
        policy = MaterializationPolicy(full_load_max_rows=10, slice_max_rows=10)
        assert policy.allows_full_load(None) is True
        assert policy.allows_slice(None) is True

    def test_limits(self):
        policy = MaterializationPolicy(
            full_load_max_rows=10, slice_max_rows=5, semi_join_max_keys=2
        )
        assert policy.allows_full_load(10) and not policy.allows_full_load(11)
        assert policy.allows_slice(5) and not policy.allows_slice(6)
        assert policy.allows_semi_join(2) and not policy.allows_semi_join(3)

    def test_as_dict_round_trip(self):
        policy = MaterializationPolicy(full_load_max_rows=1)
        assert MaterializationPolicy(**policy.as_dict()) == policy


class TestTargetPlan:
    def test_defaults_to_a_full_load(self):
        plan = TargetPlan(REF)
        assert plan.action == "load_full"
        assert plan.table_name == "db.events"
        assert plan.needs_fetch

    def test_slice_target_uses_the_slice_table(self):
        spec = SliceSpec(predicates=(Predicate("day", "gt", 100),))
        plan = TargetPlan(REF, spec, "load_slice", estimate=9_000_000, slice_estimate=1_000)
        assert plan.table_name == spec.table_name_for(REF)
        assert plan.as_dict()["slice"] == "where day > 100"
        assert plan.as_dict()["estimate"] == 9_000_000

    def test_reuse_points_at_the_existing_slice(self):
        loaded = LoadedSlice(REF, SliceSpec.FULL, "db.events", row_count=3)
        plan = TargetPlan(REF, SliceSpec(columns=frozenset({"id"})), "reuse", covering=loaded)
        assert plan.table_name == "db.events"
        assert not plan.needs_fetch

    def test_refusal_is_not_a_fetch(self):
        assert not TargetPlan(REF, action="refuse", reason="too big").needs_fetch

    def test_semi_join_is_described(self):
        spec = SemiJoinSpec(TableRef("db", "users"), "db.users", "id", "user_id")
        plan = TargetPlan(REF, action="semi_join", semi_join=spec)
        assert plan.needs_fetch
        assert plan.as_dict()["semi_join"] == "db.users.id -> user_id"


class TestMessages:
    def test_refusal_names_the_table_size_and_every_way_out(self):
        message = refusal_message(REF, 9_000_000, MaterializationPolicy())
        assert "db.events" in message
        assert "9,000,000 rows estimated" in message
        assert "full_load_max_rows=500,000" in message
        assert "WHERE" in message and "events columns" in message
        assert "only the columns you need" in message
        assert "equality key" in message
        assert "load_table('db.events', where='...', columns=[...])" in message
        assert "FUSION_FULL_LOAD_MAX_ROWS" in message

    def test_refusal_without_an_estimate(self):
        assert "size unknown" in refusal_message(REF, None, MaterializationPolicy())

    def test_refusal_can_carry_extra_hints(self):
        message = refusal_message(REF, 1, MaterializationPolicy(), hints=["drop the ORDER BY"])
        assert "drop the ORDER BY" in message

    def test_budget_message(self):
        message = budget_message(3_000_000, MaterializationPolicy())
        assert "3,000,000" in message
        assert "slice_budget_rows=2,000,000" in message
        assert "FUSION_SLICE_BUDGET_ROWS" in message
