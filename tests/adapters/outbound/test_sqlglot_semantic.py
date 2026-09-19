"""Tests for the semantic SQL compiler.

Compiled SQL is asserted three ways: it passes the same guardrails as SQL from
a caller, the analyzer reads the declared shape back out of the base scan, and
the numbers come from executing it on a real DuckDB store.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.adapters.outbound.sqlglot_semantic import SqlglotSemanticCompiler
from fusion.domain.errors import QueryError
from fusion.domain.measures import infer_model
from fusion.domain.metric_dsl import parse_metric
from fusion.domain.models import ColumnInfo, TableRef, TableSchema
from fusion.domain.semantic_query import SemanticQuery, parse_dimension
from fusion.domain.slices import Predicate
from fusion.domain.sql_text import has_multiple_statements

REF = TableRef(source="shop", table="orders")
COLUMNS = [
    ("order_date", "DATE"),
    ("amount", "DOUBLE"),
    ("quantity", "INTEGER"),
    ("status", "VARCHAR"),
]
# January has two orders, February one, March none, April one. The March gap is
# what separates time_shift from lag.
ROWS = [
    ("2024-01-15", 10.0, 1, "paid"),
    ("2024-01-20", 5.0, 2, "paid"),
    ("2024-02-10", 20.0, 2, "paid"),
    ("2024-04-05", 40.0, 4, "paid"),
    ("2024-04-06", 1.0, 1, "void"),
]


@pytest.fixture
def schema() -> TableSchema:
    return TableSchema(columns=[ColumnInfo(name=n, type=t) for n, t in COLUMNS])


@pytest.fixture
def model(schema: TableSchema):
    return infer_model(REF, schema)


@pytest.fixture
def compiler() -> SqlglotSemanticCompiler:
    return SqlglotSemanticCompiler()


@pytest.fixture
def store() -> DuckDBStore:
    # A source is a DuckDB schema, exactly as SourceService creates it.
    store = DuckDBStore()
    store.create_schema("shop")
    store.execute(
        'CREATE TABLE "shop"."orders" '
        "(order_date DATE, amount DOUBLE, quantity INTEGER, status VARCHAR)"
    )
    for row in ROWS:
        store.execute('INSERT INTO "shop"."orders" VALUES (?, ?, ?, ?)', list(row))
    yield store
    store.close()


def build(model, metrics, dimensions=("order_date:month",), filters=(), **kwargs):
    return SemanticQuery(
        model=model,
        metrics=tuple(parse_metric(m) for m in metrics),
        dimensions=tuple(parse_dimension(d, model) for d in dimensions),
        filters=tuple(filters),
        **kwargs,
    )


def run(store: DuckDBStore, compiler, query: SemanticQuery):
    """Compile and execute, returning the rows."""
    compiled = compiler.compile(query, {REF: "shop.orders"})
    SqlglotValidator().validate(compiled.sql)
    return store.execute(compiled.sql).rows


MONTHLY = ["amount:sum"]
ALL_METRICS = [
    "amount:sum",
    "amount:avg",
    "amount:min",
    "amount:max",
    "amount:median",
    "*:count",
    "status:count_distinct",
    "amount:weighted_avg(weight=quantity)",
    "cumsum(amount:sum)",
    "change(amount:sum)",
    "change_pct(amount:sum)",
    "lag(amount:sum)",
    "lead(amount:sum)",
    "time_shift(amount:sum)",
    "rank(amount:sum)",
    "dense_rank(amount:sum)",
    "percent_rank(amount:sum)",
    "ntile(amount:sum, buckets=2)",
    "change_pct(cumsum(amount:sum))",
    "change(cumsum(lag(amount:sum)))",
]


class TestGuardrails:
    @pytest.mark.parametrize("metric", ALL_METRICS)
    def test_every_metric_compiles_to_sql_the_validator_accepts(self, model, compiler, metric):
        # Generated SQL goes through exactly the guardrails caller SQL does.
        compiled = compiler.compile(build(model, [metric]))
        SqlglotValidator().validate(compiled.sql)
        SqlglotValidator().validate(compiled.base_sql)

    @pytest.mark.parametrize("metric", ALL_METRICS)
    def test_every_identifier_is_quoted(self, model, compiler, metric):
        # Quoting is a guardrail as well as correctness: the forbidden-function
        # check matches `name(`, which a quoted "load" can never produce.
        sql = compiler.compile(build(model, [metric])).sql
        for name in ("order_date", "amount", "base", "w_0"):
            assert f" {name} " not in f" {sql} " or f'"{name}"' in sql

    def test_a_column_named_like_a_forbidden_function_cannot_smuggle_a_call(self, compiler):
        schema = TableSchema(
            columns=[ColumnInfo(name="read_csv", type="DOUBLE"), ColumnInfo(name="g", type="DATE")]
        )
        model = infer_model(REF, schema)
        compiled = compiler.compile(build(model, ["read_csv:sum"], dimensions=("g",)))
        assert '"read_csv"' in compiled.sql
        assert "read_csv(" not in compiled.sql
        SqlglotValidator().validate(compiled.sql)

    def test_a_string_filter_value_is_escaped_not_interpolated(self, model, compiler):
        nasty = "'; DROP TABLE orders; --"
        compiled = compiler.compile(
            build(model, MONTHLY, filters=[Predicate(column="status", op="eq", value=nasty)])
        )
        SqlglotValidator().validate(compiled.sql)
        # The quote is doubled, so the payload stays one string literal...
        assert not has_multiple_statements(compiled.sql)
        assert len(sqlglot.parse(compiled.sql, dialect="duckdb")) == 1
        # ...and it survives as exactly the value that was asked for.
        where = sqlglot.parse_one(compiled.sql, dialect="duckdb").find(exp.Where)
        assert where.find(exp.Literal).this == nasty


class TestShapeOracle:
    """The analyzer is the independent check on the shape the query declared."""

    @pytest.mark.parametrize("metric", ALL_METRICS)
    def test_the_analyzer_reads_the_declared_shape_out_of_the_base_scan(
        self, model, compiler, metric
    ):
        query = build(
            model,
            [metric],
            filters=[Predicate(column="status", op="eq", value="paid")],
        )
        declared = query.query_shape()
        observed = SqlglotAnalyzer().analyze(compiler.compile(query).base_sql)

        assert observed.is_simple_select, "the base scan must stay analyzable"
        theirs = observed.tables[0]
        ours = declared.tables[0]
        # Every column the SQL reads was declared...
        assert theirs.columns is not None
        assert theirs.columns <= ours.columns
        # ...and the filters match exactly, so the slice cannot drop a row.
        assert set(theirs.predicates) == set(ours.predicates)

    def test_the_full_statement_is_what_defeats_the_analyzer(self, model, compiler):
        # This is the whole reason the shape is derived rather than parsed:
        # the analyzer gives up on the statement, but not on its base scan.
        compiled = compiler.compile(build(model, ["cumsum(amount:sum)"]))
        assert not SqlglotAnalyzer().analyze(compiled.sql).is_simple_select
        assert SqlglotAnalyzer().analyze(compiled.base_sql).is_simple_select


class TestStructure:
    def test_one_cte_per_nesting_level(self, model, compiler):
        assert compiler.compile(build(model, ["amount:sum"])).sql.count(" AS (") == 1
        assert compiler.compile(build(model, ["cumsum(amount:sum)"])).sql.count(" AS (") == 2
        assert (
            compiler.compile(build(model, ["change_pct(cumsum(amount:sum))"])).sql.count(" AS (")
            == 3
        )

    def test_metrics_of_different_depths_are_carried_through(self, model, compiler):
        compiled = compiler.compile(build(model, ["change_pct(cumsum(amount:sum))", "*:count"]))
        assert compiled.columns == ("order_date", "change_pct_cumsum_amount_sum", "count")
        # The shallow metric survives to the final SELECT unchanged.
        assert '"count"' in compiled.sql.rsplit("SELECT", 1)[1]

    def test_the_slice_name_is_written_in_not_rewritten_afterwards(self, model, compiler):
        compiled = compiler.compile(build(model, MONTHLY), {REF: "shop.orders__s_ab12cd34"})
        assert '"shop"."orders__s_ab12cd34"' in compiled.sql
        assert "__s_ab12cd34" in compiled.base_sql

    def test_an_unmapped_table_falls_back_to_its_own_name(self, model, compiler):
        assert '"shop"."orders"' in compiler.compile(build(model, MONTHLY)).sql

    def test_the_limit_is_an_integer(self, model, compiler):
        assert "LIMIT 7" in compiler.compile(build(model, MONTHLY, limit=7)).sql


class TestNumbers:
    """Numbers, from executing the generated SQL on a real store."""

    def values(self, store, compiler, model, metric, **kwargs):
        rows = run(store, compiler, build(model, [metric], **kwargs))
        return [row[1] for row in rows]

    def test_aggregations(self, store, compiler, model):
        paid = {"filters": [Predicate(column="status", op="eq", value="paid")]}
        assert self.values(store, compiler, model, "amount:sum", **paid) == [15.0, 20.0, 40.0]
        assert self.values(store, compiler, model, "amount:avg", **paid) == [7.5, 20.0, 40.0]
        assert self.values(store, compiler, model, "amount:min", **paid) == [5.0, 20.0, 40.0]
        assert self.values(store, compiler, model, "amount:max", **paid) == [10.0, 20.0, 40.0]
        assert self.values(store, compiler, model, "amount:median", **paid) == [7.5, 20.0, 40.0]
        assert self.values(store, compiler, model, "*:count", **paid) == [2, 1, 1]

    def test_count_distinct_and_weighted_avg(self, store, compiler, model):
        assert self.values(store, compiler, model, "status:count_distinct") == [1, 1, 2]
        # January: (10*1 + 5*2) / (1+2) = 20/3
        weighted = self.values(store, compiler, model, "amount:weighted_avg(weight=quantity)")
        assert weighted[0] == pytest.approx(20 / 3)

    def test_a_filter_changes_the_numbers(self, store, compiler, model):
        assert self.values(store, compiler, model, "*:count") == [2, 1, 2]
        assert self.values(
            store,
            compiler,
            model,
            "*:count",
            filters=[Predicate(column="status", op="eq", value="paid")],
        ) == [2, 1, 1]

    def test_sequence_transforms(self, store, compiler, model):
        paid = {"filters": [Predicate(column="status", op="eq", value="paid")]}
        assert self.values(store, compiler, model, "cumsum(amount:sum)", **paid) == [
            15.0,
            35.0,
            75.0,
        ]
        assert self.values(store, compiler, model, "change(amount:sum)", **paid) == [
            None,
            5.0,
            20.0,
        ]
        assert self.values(store, compiler, model, "lead(amount:sum)", **paid) == [
            20.0,
            40.0,
            None,
        ]

    def test_time_shift_is_not_lag(self, store, compiler, model):
        # April's previous *month* is March, which has no row. lag answers
        # February's 20.0; time_shift answers nothing, which is the truth.
        paid = {"filters": [Predicate(column="status", op="eq", value="paid")]}
        assert self.values(store, compiler, model, "lag(amount:sum)", **paid) == [
            None,
            15.0,
            20.0,
        ]
        assert self.values(store, compiler, model, "time_shift(amount:sum)", **paid) == [
            None,
            15.0,
            None,
        ]

    def test_ranking_transforms(self, store, compiler, model):
        paid = {"filters": [Predicate(column="status", op="eq", value="paid")]}
        assert self.values(store, compiler, model, "rank(amount:sum)", **paid) == [3, 2, 1]
        assert self.values(store, compiler, model, "dense_rank(amount:sum)", **paid) == [3, 2, 1]
        assert self.values(store, compiler, model, "percent_rank(amount:sum)", **paid) == [
            1.0,
            0.5,
            0.0,
        ]
        assert self.values(store, compiler, model, "ntile(amount:sum, buckets=2)", **paid) == [
            2,
            1,
            1,
        ]

    def test_change_pct_of_a_zero_base_is_null_not_an_error(self, store, compiler, model):
        store.execute("INSERT INTO \"shop\".\"orders\" VALUES ('2024-05-01', 0.0, 1, 'zero')")
        store.execute("INSERT INTO \"shop\".\"orders\" VALUES ('2024-06-01', 5.0, 1, 'zero')")
        values = self.values(
            store,
            compiler,
            model,
            "change_pct(amount:sum)",
            filters=[Predicate(column="status", op="eq", value="zero")],
        )
        assert values == [None, None]

    def test_nesting_composes(self, store, compiler, model):
        paid = {"filters": [Predicate(column="status", op="eq", value="paid")]}
        # Running totals 15, 35, 75 -> changes of +133% and +114%.
        values = self.values(store, compiler, model, "change_pct(cumsum(amount:sum))", **paid)
        assert values[0] is None
        assert values[1] == pytest.approx(20 / 15)
        assert values[2] == pytest.approx(40 / 35)

    def test_ordering_and_limit(self, store, compiler, model):
        rows = run(
            store,
            compiler,
            build(model, ["amount:sum"], order_by="-amount_sum", limit=2),
        )
        assert [row[1] for row in rows] == [41.0, 20.0]

    def test_a_query_with_no_dimensions_returns_one_row(self, store, compiler, model):
        rows = run(store, compiler, build(model, ["amount:sum"], dimensions=()))
        assert rows == [(76.0,)]

    def test_a_windowed_metric_with_no_dimensions_still_runs(self, store, compiler, model):
        # One row, so the window is trivial — but it must not fail to compile.
        rows = run(store, compiler, build(model, ["cumsum(amount:sum)"], dimensions=()))
        assert rows == [(76.0,)]

    def test_grouping_by_a_plain_column(self, store, compiler, model):
        rows = run(store, compiler, build(model, ["*:count"], dimensions=("status",)))
        assert sorted(rows) == [("paid", 4), ("void", 1)]


def test_compiling_something_impossible_says_so(model, compiler):
    query = build(model, ["amount:sum"])
    broken = SqlglotSemanticCompiler()
    with pytest.raises(QueryError, match="did not parse"):
        broken._canonical("SELECT FROM WHERE ORDER (")
    # ...and the real path does not.
    assert compiler.compile(query).sql
