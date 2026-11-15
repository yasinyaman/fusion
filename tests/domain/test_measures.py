"""Tests for the semantic model: measures, dimensions and inference."""

import pytest

from fusion.domain.errors import QueryError
from fusion.domain.identifiers import ALLOWED_AGG_FUNCS
from fusion.domain.measures import (
    ROW_MEASURE,
    SEMANTIC_AGGS,
    TIME_GRAINS,
    Dimension,
    Measure,
    SemanticModel,
    check_agg_args,
    infer_model,
    normalize_agg,
)
from fusion.domain.models import ColumnInfo, TableRef, TableSchema


def schema_of(*columns: tuple[str, str]) -> TableSchema:
    return TableSchema(columns=[ColumnInfo(name=n, type=t) for n, t in columns])


class TestAggregationSets:
    def test_semantic_aggs_extend_the_tool_whitelist(self):
        # ALLOWED_AGG_FUNCS is the published aggregate_data contract and must
        # not move; the DSL's set is a superset, and the relation is asserted
        # rather than duplicated.
        assert ALLOWED_AGG_FUNCS <= SEMANTIC_AGGS
        assert SEMANTIC_AGGS - ALLOWED_AGG_FUNCS == {"COUNT_DISTINCT", "MEDIAN", "WEIGHTED_AVG"}

    def test_the_extra_names_are_not_sql_functions(self):
        # COUNT_DISTINCT(x) and WEIGHTED_AVG(x) do not exist, which is exactly
        # why they cannot join a set whose members get interpolated as
        # f"{func}({column})".
        assert not {"COUNT_DISTINCT", "WEIGHTED_AVG"} & ALLOWED_AGG_FUNCS

    def test_normalize_is_case_insensitive(self):
        assert normalize_agg("sum", "revenue") == "SUM"
        assert normalize_agg("Count_Distinct", "id") == "COUNT_DISTINCT"

    def test_an_unknown_aggregation_suggests_and_lists(self):
        with pytest.raises(QueryError) as excinfo:
            normalize_agg("summ", "revenue")
        message = str(excinfo.value)
        assert "Did you mean 'sum'?" in message
        assert "weighted_avg" in message


class TestAggregationArguments:
    def test_weighted_avg_needs_a_weight(self):
        with pytest.raises(QueryError, match="needs a 'weight' argument"):
            check_agg_args("WEIGHTED_AVG", (), "price")

    def test_weighted_avg_takes_a_column_name(self):
        assert check_agg_args("WEIGHTED_AVG", (("weight", "quantity"),), "price") == {
            "weight": "quantity"
        }

    def test_a_weight_that_is_not_a_column_is_refused(self):
        with pytest.raises(QueryError, match="must name a column"):
            check_agg_args("WEIGHTED_AVG", (("weight", 2),), "price")

    def test_other_aggregations_take_no_arguments(self):
        assert check_agg_args("SUM", (), "revenue") == {}
        with pytest.raises(QueryError, match="Accepted arguments: none"):
            check_agg_args("SUM", (("weight", "q"),), "revenue")

    def test_an_unknown_argument_names_the_accepted_one(self):
        with pytest.raises(QueryError, match="Accepted arguments: weight"):
            check_agg_args("WEIGHTED_AVG", (("w", "quantity"),), "price")


class TestMeasureChecks:
    def test_a_numeric_aggregation_needs_a_numeric_column(self):
        text = Measure(name="status", column="status", numeric=False)
        assert text.check_agg("count") == "COUNT"
        assert text.check_agg("max") == "MAX"
        with pytest.raises(QueryError, match="needs a numeric column"):
            text.check_agg("sum")

    def test_rows_can_only_be_counted(self):
        rows = Measure(name=ROW_MEASURE, column=ROW_MEASURE, default_agg="COUNT")
        assert rows.is_row_count
        assert rows.check_agg("count") == "COUNT"
        with pytest.raises(QueryError, match="only takes count"):
            rows.check_agg("sum")


class TestModelLookup:
    def model(self) -> SemanticModel:
        return SemanticModel(
            ref=TableRef(source="shop", table="orders"),
            measures=(Measure(name="revenue", column="amount"),),
            dimensions=(Dimension(name="status", column="status"),),
        )

    def test_known_names_resolve(self):
        model = self.model()
        assert model.measure("revenue").column == "amount"
        assert model.dimension("status").column == "status"

    def test_an_unknown_measure_suggests_and_points_at_list_metrics(self):
        with pytest.raises(QueryError) as excinfo:
            self.model().measure("revenu")
        message = str(excinfo.value)
        assert "Did you mean 'revenue'?" in message
        assert "list_metrics('shop.orders')" in message

    def test_an_unknown_dimension_suggests_and_points_at_list_metrics(self):
        with pytest.raises(QueryError) as excinfo:
            self.model().dimension("statuss")
        assert "Did you mean 'status'?" in str(excinfo.value)

    def test_an_empty_model_says_none_rather_than_nothing(self):
        empty = SemanticModel(ref=TableRef(source="s", table="t"))
        with pytest.raises(QueryError, match="Available measures: none"):
            empty.measure("x")
        with pytest.raises(QueryError, match="Available dimensions: none"):
            empty.dimension("x")


class TestInference:
    def test_numeric_columns_become_summable_measures(self):
        model = infer_model(
            TableRef(source="shop", table="orders"),
            schema_of(("amount", "DECIMAL(10,2)"), ("quantity", "BIGINT")),
        )
        assert model.measure("amount").default_agg == "SUM"
        assert model.measure("amount").numeric is True
        assert model.measure("quantity").numeric is True

    def test_text_columns_become_countable_measures_and_dimensions(self):
        model = infer_model(
            TableRef(source="shop", table="orders"), schema_of(("status", "VARCHAR"))
        )
        assert model.measure("status").numeric is False
        assert model.measure("status").default_agg == "COUNT_DISTINCT"
        assert model.dimension("status").temporal is False

    @pytest.mark.parametrize(
        "column_type", ["DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIME"]
    )
    def test_temporal_columns_carry_grains(self, column_type):
        model = infer_model(
            TableRef(source="shop", table="orders"), schema_of(("order_date", column_type))
        )
        dimension = model.dimension("order_date")
        assert dimension.temporal is True
        assert dimension.as_dict()["grains"] == list(TIME_GRAINS)

    def test_an_interval_is_not_numeric(self):
        # "interval" contains "int", so the hint match alone would be wrong.
        model = infer_model(TableRef(source="s", table="t"), schema_of(("elapsed", "INTERVAL")))
        assert model.measure("elapsed").numeric is False

    def test_the_row_measure_is_always_present(self):
        model = infer_model(TableRef(source="s", table="t"), schema_of(("a", "VARCHAR")))
        assert model.measure(ROW_MEASURE).is_row_count
        # ...even for a table with no columns at all.
        assert infer_model(TableRef(source="s", table="t"), schema_of()).measure_names == (
            ROW_MEASURE,
        )

    def test_numeric_columns_are_also_offered_as_dimensions(self):
        # Grouping by an amount is unusual, not wrong; refusing it would surprise.
        model = infer_model(TableRef(source="s", table="t"), schema_of(("amount", "INTEGER")))
        assert "amount" in model.dimension_names

    def test_as_dict_round_trips_the_model(self):
        model = infer_model(
            TableRef(source="shop", table="orders"),
            schema_of(("amount", "INTEGER"), ("order_date", "DATE")),
        )
        described = model.as_dict()
        assert described["table"] == "shop.orders"
        assert described["source"] == "inferred"
        assert {m["name"] for m in described["measures"]} == {ROW_MEASURE, "amount", "order_date"}
        assert described["measures"][0]["default_aggregation"] == "count"
