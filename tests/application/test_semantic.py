"""Tests for the SemanticService orchestration.

A FakeSemanticCompiler records what it was handed and returns fixed SQL, so
the ordering of the steps is tested without sqlglot in the way; the real
compiler is exercised in its own adapter tests.
"""

from __future__ import annotations

import logging

import pytest

from fusion.application.semantic import SemanticService
from fusion.domain.errors import QueryError
from fusion.domain.measures import Dimension, Measure
from fusion.domain.models import TableRef
from fusion.domain.query_shape import UNKNOWN_SHAPE, QueryShape, TableUse
from fusion.domain.semantic_query import CompiledQuery
from fusion.domain.slices import Predicate

TABLE = "warp_main.orders"


class FakeSemanticCompiler:
    """Records the query it compiled; returns SQL that reads a real table."""

    def __init__(self, sql: str = "", base_sql: str = "") -> None:
        self.calls: list[tuple] = []
        self._sql = sql or 'SELECT 1 AS "x"'
        self._base_sql = base_sql or 'SELECT "f"."amount" FROM "warp_main"."orders" AS "f"'

    def compile(self, query, tables=None):
        self.calls.append((query, dict(tables or {})))
        return CompiledQuery(sql=self._sql, base_sql=self._base_sql, columns=("x",))


@pytest.fixture
def semantic(app_lazy):
    return app_lazy.semantic


class TestModels:
    def test_a_model_is_inferred_from_the_catalog(self, semantic):
        model = semantic.model_for(TABLE)
        assert model.source == "inferred"
        assert "amount" in model.measure_names
        assert "product" in model.dimension_names

    def test_inference_is_memoized(self, semantic):
        assert semantic.model_for(TABLE) is semantic.model_for(TABLE)

    def test_an_unqualified_name_resolves_like_sql_does(self, semantic):
        assert semantic.model_for("orders").ref == TableRef("warp_main", "orders")

    def test_an_unknown_table_names_the_connected_ones(self, semantic):
        with pytest.raises(QueryError, match="warp_main.orders"):
            semantic.model_for("warp_main.nope")

    def test_an_unsafe_table_name_is_refused_before_anything_else(self, semantic):
        with pytest.raises(QueryError, match="Invalid table name"):
            semantic.model_for("orders; DROP TABLE users")

    def test_define_replaces_the_inferred_model(self, semantic):
        semantic.model_for(TABLE)  # populate the inference cache first
        model = semantic.define(
            TABLE,
            measures=[Measure(name="revenue", column="amount")],
            dimensions=[Dimension(name="sku", column="product")],
        )
        assert model.source == "explicit"
        assert semantic.model_for(TABLE) is model
        assert semantic.model_for(TABLE).measure_names == ("revenue",)

    def test_a_configured_model_wins_over_inference(self, app_lazy):
        configured = SemanticService(
            app_lazy.catalog,
            app_lazy.query,
            FakeSemanticCompiler(),
            models={TABLE: app_lazy.semantic.define(TABLE, [Measure("r", "amount")], [])},
        )
        assert configured.model_for(TABLE).measure_names == ("r",)


class TestDescribe:
    def test_one_table(self, semantic):
        described = semantic.describe(TABLE)
        assert described["table"] == TABLE
        assert {t["name"] for t in described["transforms"]} >= {"cumsum", "ntile"}
        assert described["examples"][0] == "*:count"

    def test_examples_skip_key_columns(self, semantic):
        # id:sum parses and is useless; an example that reads as nonsense
        # teaches the wrong thing about the language.
        examples = semantic.describe(TABLE)["examples"]
        assert "amount:sum" in examples
        assert not any(e.startswith(("id:", "user_id:")) for e in examples)

    def test_everything(self, semantic):
        described = semantic.describe()
        assert TABLE in [t["table"] for t in described["tables"]]


class TestBuildAndValidate:
    def test_metrics_must_be_a_list_not_a_string(self, semantic):
        with pytest.raises(QueryError, match="must be a list"):
            semantic.build(TABLE, "amount:sum")

    def test_at_least_one_metric_is_required(self, semantic):
        with pytest.raises(QueryError, match="at least one metric"):
            semantic.build(TABLE, [])

    def test_a_limit_above_the_cap_is_clamped(self, semantic):
        assert semantic.build(TABLE, ["amount:sum"], limit=10_000).limit == 100

    def test_an_unknown_column_in_a_filter_is_caught(self, semantic):
        with pytest.raises(QueryError, match="has no column"):
            semantic.build(TABLE, ["amount:sum"], filters=[{"column": "nope", "value": 1}])

    def test_time_shift_without_a_grain_says_what_to_add(self, semantic):
        with pytest.raises(QueryError, match="dimensions=\\['order_date:month'\\]"):
            semantic.build(TABLE, ["time_shift(amount:sum)"])

    def test_ordering_by_something_absent_lists_the_columns(self, semantic):
        with pytest.raises(QueryError, match="not one of this query's columns"):
            semantic.build(TABLE, ["amount:sum"], order_by="nope")


class TestFilters:
    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ({}, "needs a 'column'"),
            ({"column": ""}, "needs a 'column'"),
            ({"column": "amount", "op": "between", "value": 1}, "Unknown filter operator"),
            ({"column": "amount", "op": "gt"}, "needs a 'value'"),
            ({"column": "a;b", "value": 1}, "Invalid column name"),
            ("not-a-mapping", "must be an object"),
        ],
    )
    def test_malformed_filters_say_what_is_wrong(self, semantic, raw, fragment):
        with pytest.raises(QueryError, match=fragment):
            semantic.build(TABLE, ["amount:sum"], filters=[raw])

    def test_is_null_needs_no_value(self, semantic):
        query = semantic.build(
            TABLE, ["amount:sum"], filters=[{"column": "amount", "op": "is_null"}]
        )
        assert query.filters == (Predicate(column="amount", op="is_null", value=False),)

    def test_in_takes_a_list(self, semantic):
        query = semantic.build(
            TABLE, ["amount:sum"], filters=[{"column": "product", "op": "in", "value": ["A", "B"]}]
        )
        assert query.filters[0].value == ("A", "B")

    def test_in_with_a_bare_string_is_refused(self, semantic):
        with pytest.raises(QueryError, match="Invalid filter"):
            semantic.build(
                TABLE, ["amount:sum"], filters=[{"column": "product", "op": "in", "value": "A"}]
            )


class TestOrchestration:
    def service(self, app_lazy, compiler, **kwargs):
        return SemanticService(app_lazy.catalog, app_lazy.query, compiler, **kwargs)

    def test_the_compiler_is_given_the_materialized_table_names(self, app_lazy):
        compiler = FakeSemanticCompiler()
        service = self.service(app_lazy, compiler)
        service.run(TABLE, ["amount:sum"], ["product"])
        query, tables = compiler.calls[-1]
        assert query.model.ref == TableRef("warp_main", "orders")
        # The table was loaded before compiling, and named as the store has it.
        assert TableRef("warp_main", "orders") in tables

    def test_explain_returns_the_plan_without_running_it(self, app_lazy):
        service = self.service(app_lazy, FakeSemanticCompiler())
        explained = service.explain(TABLE, ["cumsum(amount:sum)"], ["product"])
        assert explained["sql"]
        assert explained["base_sql"]
        assert explained["shape"]["is_aggregated"] is True
        assert explained["shape"]["limit"] is None
        assert explained["query"]["metrics"] == ["cumsum(amount:sum)"]
        assert TABLE in explained["reading"]

    def test_a_disagreeing_scan_is_logged_not_silently_accepted(self, app_lazy, caplog):
        # The dangerous direction: the query declared a filter, so the slice
        # holds only product='A' rows — but the generated SQL does not filter,
        # so it wants rows the slice never fetched. That is the failure this
        # check exists for, and it must be loud.
        compiler = FakeSemanticCompiler(
            base_sql='SELECT "f"."amount" FROM "warp_main"."orders" AS "f"'
        )
        service = self.service(app_lazy, compiler)
        with caplog.at_level(logging.WARNING):
            service.run(
                TABLE,
                ["amount:sum"],
                ["product"],
                filters=[{"column": "product", "value": "A"}],
            )
        assert "disagrees with the planned shape" in caplog.text

    def test_an_extra_filter_in_the_generated_scan_is_not_a_disagreement(self, app_lazy, caplog):
        # The other direction only narrows what the SQL reads out of a slice
        # that already holds more, so it cannot drop a row.
        compiler = FakeSemanticCompiler(
            base_sql='SELECT "f"."amount" FROM "warp_main"."orders" AS "f" '
            'WHERE "f"."product" = \'A\''
        )
        service = self.service(app_lazy, compiler)
        with caplog.at_level(logging.WARNING):
            service.run(TABLE, ["amount:sum"], ["product"])
        assert "disagrees" not in caplog.text

    def test_an_agreeing_scan_logs_nothing(self, app_lazy, caplog):
        from fusion.adapters.outbound.sqlglot_semantic import SqlglotSemanticCompiler

        service = self.service(app_lazy, SqlglotSemanticCompiler())
        with caplog.at_level(logging.WARNING):
            service.run(TABLE, ["amount:sum"], ["product"])
        assert "disagrees" not in caplog.text

    def test_the_cross_check_can_be_turned_off(self, app_lazy, caplog):
        compiler = FakeSemanticCompiler(base_sql="SELECT 1")
        service = self.service(app_lazy, compiler, cross_check=False)
        with caplog.at_level(logging.WARNING):
            service.run(TABLE, ["amount:sum"], ["product"])
        assert "disagrees" not in caplog.text

    def test_a_table_no_source_holds_is_reported(self, app_lazy, monkeypatch):
        service = self.service(app_lazy, FakeSemanticCompiler())
        monkeypatch.setattr(app_lazy.query, "materialize_for", lambda shape: {})
        with pytest.raises(QueryError, match="No connected source"):
            service.run(TABLE, ["amount:sum"])


class TestShapeMerging:
    """merge_conservative must never narrow what gets fetched."""

    def shape(self, columns, predicates=()):
        return QueryShape(
            tables=(
                TableUse(
                    ref=TableRef("s", "t"),
                    alias="f",
                    columns=None if columns is None else frozenset(columns),
                    predicates=tuple(predicates),
                ),
            ),
            is_simple_select=True,
        )

    def test_columns_are_unioned(self):
        merged = self.shape(["a"]).merge_conservative(self.shape(["b"]))
        assert merged.tables[0].columns == frozenset({"a", "b"})

    def test_all_columns_wins(self):
        assert self.shape(["a"]).merge_conservative(self.shape(None)).tables[0].columns is None
        assert self.shape(None).merge_conservative(self.shape(["a"])).tables[0].columns is None

    def test_only_agreed_predicates_survive(self):
        mine = Predicate(column="a", op="eq", value=1)
        theirs = Predicate(column="b", op="eq", value=2)
        merged = self.shape(["a"], [mine, theirs]).merge_conservative(self.shape(["a"], [mine]))
        assert merged.tables[0].predicates == (mine,)

    def test_an_analyzer_that_gave_up_teaches_nothing(self):
        mine = self.shape(["a"], [Predicate(column="a", op="eq", value=1)])
        assert mine.merge_conservative(UNKNOWN_SHAPE) == mine

    def test_a_table_the_other_shape_never_saw_is_untouched(self):
        mine = self.shape(["a"])
        other = QueryShape(
            tables=(TableUse(ref=TableRef("s", "other"), columns=frozenset({"z"})),),
            is_simple_select=True,
        )
        assert mine.merge_conservative(other).tables[0].columns == frozenset({"a"})


def test_the_tool_layer_reports_a_missing_semantic_service(app_lazy):
    from fusion.application.tools import ToolService

    without = ToolService(
        app_lazy.query,
        app_lazy.sources,
        app_lazy.views,
        app_lazy.store,
        app_lazy.catalog,
        app_lazy.cache,
    )
    assert (
        "not available"
        in without.execute("query_metrics", {"table": TABLE, "metrics": []})["error"]
    )
    assert "not available" in without.execute("list_metrics")["error"]


class TestArgumentTypes:
    """Arguments come from a language model, so their types are untrusted too.

    Every one of these used to surface as an AttributeError or a TypeError
    from several frames inside the domain; the benchmark found the first one
    when a model sent `order_by` as a list.
    """

    @pytest.mark.parametrize("bad", [["revenue_sum"], {"col": "x"}, 7])
    def test_order_by_must_be_a_single_name(self, semantic, bad):
        with pytest.raises(QueryError, match="order_by must be a single column name"):
            semantic.build(TABLE, ["amount:sum"], order_by=bad)

    @pytest.mark.parametrize("bad", ["amount:sum", 7, {"a": 1}])
    def test_metrics_must_be_a_list(self, semantic, bad):
        with pytest.raises(QueryError, match="metrics must be a list"):
            semantic.build(TABLE, bad)

    @pytest.mark.parametrize("bad", ["product", 7])
    def test_dimensions_must_be_a_list(self, semantic, bad):
        with pytest.raises(QueryError, match="dimensions must be a list"):
            semantic.build(TABLE, ["amount:sum"], dimensions=bad)

    def test_a_non_name_inside_a_list_is_named(self, semantic):
        with pytest.raises(QueryError, match="Every entry in metrics must be a name"):
            semantic.build(TABLE, [{"measure": "amount"}])

    def test_filters_must_be_a_list(self, semantic):
        with pytest.raises(QueryError, match="filters must be a list"):
            semantic.build(TABLE, ["amount:sum"], filters="product = 'A'")

    def test_none_means_none_given(self, semantic):
        query = semantic.build(TABLE, ["amount:sum"], dimensions=None, filters=())
        assert query.dimensions == ()

    def test_every_refusal_is_a_query_error_the_tool_layer_maps(self, app_lazy):
        # Not an internal error: the caller gets something it can act on.
        result = app_lazy.tools.execute(
            "query_metrics",
            {"table": TABLE, "metrics": ["amount:sum"], "order_by": ["amount_sum"]},
        )
        assert "order_by must be a single column name" in result["error"]
        assert "Internal error" not in result["error"]
