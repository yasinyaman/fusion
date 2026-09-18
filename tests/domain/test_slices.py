"""Predicates, slice specs and the registry that tracks materialized slices."""

import pytest

from fusion.domain.models import TableRef
from fusion.domain.slices import (
    LoadedSlice,
    Predicate,
    SliceRegistry,
    SliceSpec,
    columns_of,
)

REF = TableRef("db", "orders")
OTHER = TableRef("db", "users")


class TestPredicate:
    def test_normalizes_in_and_is_null(self):
        assert Predicate("a", "in", [3, 1]).value == (3, 1)
        assert Predicate("a", "is_null", 1).value is True
        assert Predicate("a", "is_null").value is False

    def test_is_hashable_and_comparable(self):
        assert Predicate("a", "in", [1, 2]) == Predicate("a", "in", (1, 2))
        assert len({Predicate("a", "eq", 1), Predicate("a", "eq", 1)}) == 1

    def test_rejects_unknown_operator_and_bad_in(self):
        with pytest.raises(ValueError, match="operator"):
            Predicate("a", "between", 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="sequence"):
            Predicate("a", "in", "abc")

    @pytest.mark.parametrize(
        ("predicate", "row", "expected"),
        [
            (Predicate("s", "eq", "new"), {"s": "new"}, True),
            (Predicate("s", "eq", "new"), {"s": "old"}, False),
            (Predicate("s", "ne", "new"), {"s": "old"}, True),
            (Predicate("n", "gt", 10), {"n": 11}, True),
            (Predicate("n", "gt", 10), {"n": 10}, False),
            (Predicate("n", "gte", 10), {"n": 10}, True),
            (Predicate("n", "lt", 10), {"n": 9}, True),
            (Predicate("n", "lte", 10), {"n": 10}, True),
            (Predicate("n", "in", (1, 2)), {"n": 2}, True),
            (Predicate("n", "in", (1, 2)), {"n": 3}, False),
            (Predicate("s", "like", "a%c"), {"s": "abc"}, True),
            (Predicate("s", "like", "a_c"), {"s": "abbc"}, False),
            (Predicate("s", "like", "100%"), {"s": "100% off"}, True),
            (Predicate("s", "is_null", True), {"s": None}, True),
            (Predicate("s", "is_null", False), {"s": None}, False),
            (Predicate("s", "is_null", False), {"s": "x"}, True),
            (Predicate("s", "eq", "x"), {}, False),  # missing column
            (Predicate("n", "gt", 10), {"n": None}, False),  # NULL never matches
            (Predicate("n", "ne", 10), {"n": None}, False),
        ],
    )
    def test_matches(self, predicate, row, expected):
        assert predicate.matches(row) is expected

    def test_matches_survives_incomparable_values(self):
        assert Predicate("n", "gt", 10).matches({"n": "text"}) is False


class TestImplies:
    def test_identical_predicates(self):
        assert Predicate("a", "eq", 1).implies(Predicate("a", "eq", 1))

    def test_different_columns_never_imply(self):
        assert not Predicate("a", "eq", 1).implies(Predicate("b", "eq", 1))

    def test_eq_inside_in_list(self):
        assert Predicate("a", "eq", 2).implies(Predicate("a", "in", (1, 2, 3)))
        assert not Predicate("a", "eq", 9).implies(Predicate("a", "in", (1, 2)))

    def test_in_subset_and_singleton(self):
        assert Predicate("a", "in", (1, 2)).implies(Predicate("a", "in", (1, 2, 3)))
        assert not Predicate("a", "in", (1, 4)).implies(Predicate("a", "in", (1, 2)))
        assert Predicate("a", "in", (5,)).implies(Predicate("a", "eq", 5))

    def test_narrower_bounds(self):
        assert Predicate("n", "gt", 10).implies(Predicate("n", "gt", 5))
        assert Predicate("n", "gte", 10).implies(Predicate("n", "gt", 5))
        assert Predicate("n", "gt", 5).implies(Predicate("n", "gte", 5))
        assert not Predicate("n", "gte", 5).implies(Predicate("n", "gt", 5))
        assert not Predicate("n", "gt", 1).implies(Predicate("n", "gt", 5))
        assert Predicate("n", "lt", 5).implies(Predicate("n", "lt", 10))
        assert Predicate("n", "lte", 5).implies(Predicate("n", "lt", 10))
        assert not Predicate("n", "lt", 20).implies(Predicate("n", "lt", 10))

    def test_eq_inside_a_range(self):
        assert Predicate("n", "eq", 7).implies(Predicate("n", "gt", 5))
        assert Predicate("n", "eq", 7).implies(Predicate("n", "lte", 7))
        assert not Predicate("n", "eq", 3).implies(Predicate("n", "gt", 5))

    def test_unrelated_operators_do_not_imply(self):
        assert not Predicate("s", "like", "a%").implies(Predicate("s", "like", "%"))
        assert not Predicate("n", "ne", 1).implies(Predicate("n", "gt", 0))
        assert not Predicate("n", "gt", 1).implies(Predicate("n", "lt", 9))
        assert not Predicate("s", "is_null", False).implies(Predicate("s", "ne", "x"))

    def test_incomparable_types_do_not_imply(self):
        assert not Predicate("n", "gt", "abc").implies(Predicate("n", "gt", 5))


class TestSliceSpec:
    def test_full_slice(self):
        assert SliceSpec.FULL.is_full
        assert SliceSpec().table_suffix() == ""
        assert SliceSpec.FULL.table_name_for(REF) == "db.orders"
        assert SliceSpec.FULL.describe() == "full table"

    def test_partial_slice_naming_is_stable_and_unique(self):
        spec = SliceSpec(columns=frozenset({"id"}), predicates=(Predicate("s", "eq", "new"),))
        name = spec.table_name_for(REF)
        assert name.startswith("db.orders__s_") and len(name) == len("db.orders__s_") + 10
        assert name == spec.table_name_for(REF)
        other = SliceSpec(columns=frozenset({"id"}), predicates=(Predicate("s", "eq", "old"),))
        assert other.table_name_for(REF) != name

    def test_predicate_order_does_not_change_identity(self):
        a = Predicate("a", "eq", 1)
        b = Predicate("b", "gt", 2)
        assert SliceSpec(predicates=(a, b)).table_suffix() == (
            SliceSpec(predicates=(b, a)).table_suffix()
        )

    def test_describe(self):
        spec = SliceSpec(
            columns=frozenset({"id", "total"}),
            predicates=(Predicate("s", "eq", "new"),),
            limit=10,
        )
        assert spec.describe() == "columns=id,total where s = 'new' limit 10"
        assert SliceSpec(predicates=(Predicate("a", "is_null", True),)).describe() == (
            "where a IS NULL"
        )

    def test_with_predicate_and_matches(self):
        spec = SliceSpec(predicates=(Predicate("s", "eq", "new"),))
        wider = spec.with_predicate(Predicate("n", "gt", 1))
        assert len(wider.predicates) == 2
        assert wider.matches({"s": "new", "n": 5})
        assert not wider.matches({"s": "new", "n": 0})

    def test_as_dict(self):
        spec = SliceSpec(columns=frozenset({"b", "a"}), predicates=(Predicate("n", "in", (1, 2)),))
        assert spec.as_dict() == {
            "columns": ["a", "b"],
            "predicates": [{"column": "n", "op": "in", "value": [1, 2]}],
            "limit": None,
        }


class TestSubsumes:
    def test_full_covers_everything(self):
        narrow = SliceSpec(columns=frozenset({"id"}), predicates=(Predicate("s", "eq", "x"),))
        assert SliceSpec.FULL.subsumes(narrow)
        assert not narrow.subsumes(SliceSpec.FULL)

    def test_columns_must_be_contained(self):
        loaded = SliceSpec(columns=frozenset({"id", "total"}))
        assert loaded.subsumes(SliceSpec(columns=frozenset({"id"})))
        assert not loaded.subsumes(SliceSpec(columns=frozenset({"id", "status"})))
        assert not loaded.subsumes(SliceSpec(columns=None))

    def test_requested_must_be_at_least_as_narrow(self):
        loaded = SliceSpec(predicates=(Predicate("s", "eq", "new"),))
        assert loaded.subsumes(
            SliceSpec(predicates=(Predicate("s", "eq", "new"), Predicate("n", "gt", 1)))
        )
        assert not loaded.subsumes(SliceSpec(predicates=(Predicate("n", "gt", 1),)))
        assert not loaded.subsumes(SliceSpec.FULL)

    def test_range_containment(self):
        loaded = SliceSpec(predicates=(Predicate("d", "gt", 100),))
        assert loaded.subsumes(SliceSpec(predicates=(Predicate("d", "gt", 200),)))
        assert not loaded.subsumes(SliceSpec(predicates=(Predicate("d", "gt", 50),)))

    def test_a_limited_slice_only_matches_itself(self):
        limited = SliceSpec(predicates=(Predicate("s", "eq", "x"),), limit=10)
        assert limited.subsumes(SliceSpec(predicates=(Predicate("s", "eq", "x"),), limit=10))
        assert not limited.subsumes(SliceSpec(predicates=(Predicate("s", "eq", "x"),)))
        assert not limited.subsumes(SliceSpec(predicates=(Predicate("s", "eq", "x"),), limit=5))

    def test_a_complete_slice_answers_a_limited_request(self):
        # Taking 10 rows out of a fully loaded table is done locally.
        assert SliceSpec.FULL.subsumes(SliceSpec(limit=10))
        loaded = SliceSpec(predicates=(Predicate("s", "eq", "x"),))
        assert loaded.subsumes(SliceSpec(predicates=(Predicate("s", "eq", "x"),), limit=10))
        assert not loaded.subsumes(SliceSpec(limit=10))


def _loaded(spec, name="t", rows=10, at=1.0, **kwargs):
    return LoadedSlice(
        ref=kwargs.pop("ref", REF),
        spec=spec,
        table_name=name,
        row_count=rows,
        loaded_at=at,
        last_used=at,
        **kwargs,
    )


class TestSliceRegistry:
    def test_record_get_and_len(self):
        registry = SliceRegistry()
        loaded = registry.record(_loaded(SliceSpec.FULL, "db.orders"))
        assert len(registry) == 1
        assert "db.orders" in registry
        assert registry.get("db.orders") is loaded
        assert registry.get("nope") is None
        assert list(registry) == [loaded]

    def test_slices_are_newest_first_and_per_table(self):
        registry = SliceRegistry()
        old = registry.record(_loaded(SliceSpec(columns=frozenset({"a"})), "a", at=1.0))
        new = registry.record(_loaded(SliceSpec(columns=frozenset({"b"})), "b", at=2.0))
        registry.record(_loaded(SliceSpec.FULL, "u", ref=OTHER))
        assert registry.slices(REF) == [new, old]
        assert [s.table_name for s in registry.slices(OTHER)] == ["u"]
        assert len(registry.all()) == 3

    def test_full_slice_requires_completeness(self):
        registry = SliceRegistry()
        partial = registry.record(_loaded(SliceSpec.FULL, "db.orders", complete=False))
        assert registry.full_slice(REF) is None
        partial.complete = True
        assert registry.full_slice(REF) is partial

    def test_find_covering_prefers_full_then_smallest(self):
        registry = SliceRegistry()
        spec = SliceSpec(predicates=(Predicate("s", "eq", "new"),))
        big = registry.record(_loaded(SliceSpec.FULL, "full", rows=1000))
        small = registry.record(_loaded(spec, "small", rows=5))
        assert registry.find_covering(REF, spec) is big  # a full slice answers anything
        registry.evict("full")
        assert registry.find_covering(REF, spec) is small
        assert registry.find_covering(REF, SliceSpec.FULL) is None

    def test_find_covering_picks_the_narrowest_of_several(self):
        registry = SliceRegistry()
        spec = SliceSpec(predicates=(Predicate("s", "eq", "new"), Predicate("n", "gt", 5)))
        registry.record(_loaded(SliceSpec(predicates=(Predicate("s", "eq", "new"),)), "wide", 100))
        narrow = registry.record(_loaded(spec, "narrow", 4))
        assert registry.find_covering(REF, spec) is narrow

    def test_find_covering_skips_incomplete_slices(self):
        registry = SliceRegistry()
        registry.record(_loaded(SliceSpec.FULL, "full", complete=False))
        assert registry.find_covering(REF, SliceSpec(columns=frozenset({"a"}))) is None

    def test_find_covering_touches_the_winner(self):
        registry = SliceRegistry()
        loaded = registry.record(_loaded(SliceSpec.FULL, "full", at=1.0))
        registry.find_covering(REF, SliceSpec.FULL, now=50.0)
        assert loaded.last_used == 50.0

    def test_eviction_by_name_ref_and_source(self):
        registry = SliceRegistry()
        registry.record(_loaded(SliceSpec.FULL, "a"))
        registry.record(_loaded(SliceSpec(columns=frozenset({"x"})), "b"))
        registry.record(_loaded(SliceSpec.FULL, "u", ref=OTHER))
        registry.record(_loaded(SliceSpec.FULL, "z", ref=TableRef("other", "t")))
        assert registry.evict("a").table_name == "a"
        assert registry.evict("a") is None
        assert [s.table_name for s in registry.evict_ref(REF)] == ["b"]
        assert {s.table_name for s in registry.evict_source("db")} == {"u"}
        assert [s.table_name for s in registry.all()] == ["z"]

    def test_total_rows_and_lru_order(self):
        registry = SliceRegistry()
        registry.record(_loaded(SliceSpec.FULL, "old", rows=10, at=1.0))
        registry.record(_loaded(SliceSpec(columns=frozenset({"x"})), "new", rows=5, at=9.0))
        assert registry.total_rows() == 15
        assert [s.table_name for s in registry.lru_candidates()] == ["old", "new"]
        assert [s.table_name for s in registry.lru_candidates(protect=["old"])] == ["new"]

    def test_touch_moves_a_slice_to_the_back_of_the_lru(self):
        registry = SliceRegistry()
        registry.record(_loaded(SliceSpec.FULL, "a", at=1.0))
        registry.record(_loaded(SliceSpec(columns=frozenset({"x"})), "b", at=2.0))
        registry.touch("a", 10.0)
        assert [s.table_name for s in registry.lru_candidates()] == ["b", "a"]


class TestLoadedSlice:
    def test_as_dict_and_flags(self):
        loaded = _loaded(SliceSpec(predicates=(Predicate("s", "eq", "new"),)), "db.orders__s_x", 3)
        loaded.derived_from = "semijoin:db.users.id"
        assert loaded.as_dict() == {
            "table": "db.orders__s_x",
            "spec": {
                "columns": None,
                "predicates": [{"column": "s", "op": "eq", "value": "new"}],
                "limit": None,
            },
            "description": "where s = 'new'",
            "row_count": 3,
            "complete": True,
            "derived_from": "semijoin:db.users.id",
        }
        assert loaded.reusable and not loaded.is_full
        loaded.complete = False
        assert not loaded.reusable


def test_columns_of():
    assert columns_of(None) is None
    assert columns_of([]) is None
    assert columns_of(["b", "a", "b"]) == frozenset({"a", "b"})
