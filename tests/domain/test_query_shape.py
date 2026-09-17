"""TableUse / JoinEquality / QueryShape: what a statement needs per table."""

from fusion.domain.models import TableRef
from fusion.domain.query_shape import UNKNOWN_SHAPE, JoinEquality, QueryShape, TableUse
from fusion.domain.slices import Predicate

ORDERS = TableRef("db", "orders")
USERS = TableRef("db", "users")


class TestTableUse:
    def test_slice_spec_carries_columns_and_predicates(self):
        use = TableUse(ORDERS, "o", frozenset({"id"}), (Predicate("s", "eq", "new"),))
        spec = use.slice_spec()
        assert spec.columns == frozenset({"id"})
        assert spec.predicates == (Predicate("s", "eq", "new"),)
        assert spec.limit is None
        assert use.slice_spec(limit=5).limit == 5

    def test_unrestricted_use(self):
        assert TableUse(ORDERS, "o").is_unrestricted
        assert not TableUse(ORDERS, "o", columns=frozenset({"id"})).is_unrestricted

    def test_as_dict(self):
        use = TableUse(ORDERS, "o", frozenset({"b", "a"}), outer_null_side=True)
        assert use.as_dict() == {
            "table": "db.orders",
            "alias": "o",
            "columns": ["a", "b"],
            "predicates": [],
            "outer_null_side": True,
        }


class TestJoinEquality:
    def test_sides(self):
        join = JoinEquality("o", "user_id", "u", "id")
        assert join.other_side("o") == ("u", "id")
        assert join.other_side("u") == ("o", "user_id")
        assert join.other_side("x") is None
        assert join.column_for("o") == "user_id"
        assert join.column_for("u") == "id"
        assert join.column_for("x") is None


class TestQueryShape:
    def test_lookup_by_ref_and_alias(self):
        shape = QueryShape(tables=(TableUse(ORDERS, "o"), TableUse(USERS, "u")))
        assert shape.use_for(ORDERS).alias == "o"
        assert shape.use_for_alias("u").ref == USERS
        assert shape.use_for(TableRef("db", "nope")) is None
        assert shape.use_for_alias("nope") is None

    def test_single_table(self):
        assert QueryShape(tables=(TableUse(ORDERS, "o"),)).single_table
        assert not QueryShape(
            tables=(TableUse(ORDERS, "o"), TableUse(USERS, "u")),
            joins=(JoinEquality("o", "user_id", "u", "id"),),
        ).single_table

    def test_inner_joins_for_ignores_outer_ones(self):
        inner = JoinEquality("o", "user_id", "u", "id", inner=True)
        outer = JoinEquality("o", "x", "p", "y", inner=False)
        shape = QueryShape(tables=(TableUse(ORDERS, "o"),), joins=(inner, outer))
        assert shape.inner_joins_for("o") == (inner,)
        assert shape.inner_joins_for("p") == ()

    def test_unknown_shape_is_not_simple(self):
        assert UNKNOWN_SHAPE.is_simple_select is False
        assert UNKNOWN_SHAPE.tables == ()

    def test_as_dict(self):
        shape = QueryShape(
            tables=(TableUse(ORDERS, "o"),),
            joins=(JoinEquality("o", "user_id", "u", "id"),),
            limit=5,
        )
        assert shape.as_dict()["limit"] == 5
        assert shape.as_dict()["joins"] == [{"left": "o.user_id", "right": "u.id", "inner": True}]
