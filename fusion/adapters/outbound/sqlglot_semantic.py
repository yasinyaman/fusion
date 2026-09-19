"""Compiles a semantic query into DuckDB SQL.

The shape is always the same: a flat ``base`` CTE that reads the table and
aggregates, then one CTE per level of transform nesting, then a final SELECT
that orders and limits.

One CTE per level is not a stylistic choice — SQL has no way to put a window
over another window in the same SELECT, so anything nested at all needs them,
and being uniform about it keeps the generated SQL predictable. DuckDB
collapses them anyway.

Everything is generated for, parsed as and executed by DuckDB. That matters:
the compiled SQL must never be handed to the neutral-dialect analyzer, whose
round trip rewrites string literals and drops quoting, nor shipped to a source
by pushdown, where an interval literal or a ``NULLS LAST`` default would parse
on both sides and answer differently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from fusion.domain.errors import GuardrailViolation, QueryError
from fusion.domain.measures import ROW_MEASURE, check_agg_args
from fusion.domain.metric_dsl import MetricExpr, measure_ref_of, transforms_of
from fusion.domain.models import TableRef
from fusion.domain.semantic_query import FACT_ALIAS, CompiledQuery, SemanticQuery
from fusion.domain.slices import Predicate

DIALECT = "duckdb"

_BASE_CTE = "base"


def _quote(name: str) -> str:
    """Quote an identifier for DuckDB.

    Every identifier is quoted, which is also a guardrail: the validator's
    forbidden-function check looks for ``name(``, and a quoted ``"load"`` can
    never be followed by ``(``. A column called ``read_csv`` cannot smuggle a
    call through.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect=DIALECT)


def _literal(value: Any) -> str:
    """Render a Python value as a DuckDB literal, escaping included."""
    return exp.convert(value).sql(dialect=DIALECT)


def _qualified(table: str) -> str:
    """Quote ``schema.table``; only the first dot separates the two parts.

    Matches ``DuckDBStore._quote_qualified``, because this has to name the
    table the store actually created — a source becomes a DuckDB schema, and a
    slice's own name (``orders__s_ab12cd34``) is a single identifier inside it.
    """
    schema, separator, name = table.partition(".")
    if not separator:
        return _quote(table)
    return f"{_quote(schema)}.{_quote(name)}"


def _predicate_sql(predicate: Predicate, alias: str) -> str:
    column = f"{_quote(alias)}.{_quote(predicate.column)}"
    op = predicate.op
    if op == "is_null":
        return f"{column} IS {'' if predicate.value else 'NOT '}NULL"
    if op == "in":
        values = ", ".join(_literal(v) for v in predicate.value)
        return f"{column} IN ({values})" if values else "FALSE"
    symbols = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if op == "like":
        return f"{column} LIKE {_literal(predicate.value)}"
    return f"{column} {symbols[op]} {_literal(predicate.value)}"


def _aggregate_sql(expr: MetricExpr, query: SemanticQuery) -> str:
    """The aggregate for one metric's measure, as it appears in the base CTE."""
    ref = measure_ref_of(expr)
    measure = query.model.measure(ref.measure)
    agg = measure.check_agg(ref.agg)
    if measure.column == ROW_MEASURE:
        # `*:count` counts rows; `*:count_distinct` is the same question.
        return "COUNT(*)"
    column = f"{_quote(FACT_ALIAS)}.{_quote(measure.column)}"
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {column})"
    if agg == "WEIGHTED_AVG":
        weight_column = check_agg_args(agg, ref.args, ref.measure)["weight"]
        weight = f"{_quote(FACT_ALIAS)}.{_quote(weight_column)}"
        # NULLIF so a zero total weight is NULL rather than a division error.
        return f"SUM({column} * {weight}) / NULLIF(SUM({weight}), 0)"
    return f"{agg}({column})"


def _dimension_sql(column: str, grain: str | None) -> str:
    qualified = f"{_quote(FACT_ALIAS)}.{_quote(column)}"
    if grain is None:
        return qualified
    return f"DATE_TRUNC('{grain}', {qualified})"


class SqlglotSemanticCompiler:
    """Builds DuckDB SQL for a :class:`SemanticQuery`."""

    dialect = DIALECT

    def compile(
        self, query: SemanticQuery, tables: Mapping[TableRef, str] | None = None
    ) -> CompiledQuery:
        """Generate the SQL for ``query``.

        Args:
            query: An already-validated semantic query.
            tables: Where each table lives in the store (a slice name, usually).
                Missing entries fall back to the table's own full name.

        Returns:
            The full statement, the base scan on its own, and the output columns.

        Raises:
            QueryError: When the generated SQL does not parse as DuckDB, which
                would be a bug here rather than bad input.
        """
        source = (tables or {}).get(query.model.ref, query.model.ref.full_name)
        base_sql = self._base_sql(query, source)

        ctes: list[tuple[str, str]] = [(_BASE_CTE, base_sql)]
        previous = _BASE_CTE
        # Each metric's column name as it stands after the levels applied so far.
        current: list[str] = [measure_ref_of(m).output_name() for m in query.metrics]
        chains = [transforms_of(metric) for metric in query.metrics]

        for level in range(max((len(chain) for chain in chains), default=0)):
            name = f"w_{level}"
            ctes.append((name, self._window_sql(query, chains, current, level, previous)))
            previous = name

        select = ", ".join(_quote(column) for column in query.output_columns())
        order_column, descending = query.order_column()
        tail = (
            f"SELECT {select} FROM {_quote(previous)} "
            f"ORDER BY {_quote(order_column)} {'DESC' if descending else 'ASC'} "
            f"LIMIT {int(query.limit)}"
        )
        with_clause = ", ".join(f"{_quote(name)} AS ({body})" for name, body in ctes)
        return CompiledQuery(
            sql=self._canonical(f"WITH {with_clause} {tail}"),
            base_sql=self._canonical(base_sql),
            columns=query.output_columns(),
        )

    # -- the pieces ---------------------------------------------------------

    def _base_sql(self, query: SemanticQuery, source: str) -> str:
        """The flat scan: read the table, filter, group, aggregate.

        This is the part the SQL analyzer can read back, which is what the
        contract test uses to check the shape the query claimed.
        """
        projections = [
            f"{_dimension_sql(dim.column, dim.grain)} AS {_quote(dim.output_name())}"
            for dim in query.dimensions
        ]
        projections += [
            f"{_aggregate_sql(metric, query)} AS {_quote(measure_ref_of(metric).output_name())}"
            for metric in query.metrics
        ]
        sql = f"SELECT {', '.join(projections)} FROM {_qualified(source)} AS {_quote(FACT_ALIAS)}"
        if query.filters:
            conditions = " AND ".join(_predicate_sql(p, FACT_ALIAS) for p in query.filters)
            sql += f" WHERE {conditions}"
        if query.dimensions:
            # Group by position: the dimension expression may be a DATE_TRUNC,
            # and repeating it would be the only other option.
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(query.dimensions)))
        return sql

    def _window_sql(
        self,
        query: SemanticQuery,
        chains: Sequence[Sequence[Any]],
        current: list[str],
        level: int,
        previous: str,
    ) -> str:
        """One nesting level: apply each metric's transform, carry the rest through."""
        projections = [_quote(dim.output_name()) for dim in query.dimensions]
        for index, chain in enumerate(chains):
            if level >= len(chain):
                # This metric has no transform left; keep its column as it is.
                projections.append(_quote(current[index]))
                continue
            call = chain[level]
            inner = current[index]
            produced = f"{call.name}_{inner}"
            projections.append(f"{self._transform_sql(call, inner, query)} AS {_quote(produced)}")
            current[index] = produced
        return f"SELECT {', '.join(projections)} FROM {_quote(previous)}"

    def _transform_sql(self, call: Any, column: str, query: SemanticQuery) -> str:
        """The windowed expression for one transform over ``column``."""
        args = call.spec.bind(call.args)
        value = _quote(column)
        order = self._order_expression(query, column)

        if call.name == "cumsum":
            frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
            return f"SUM({value}) OVER (ORDER BY {order} {frame})"
        if call.name in ("lag", "lead"):
            return f"{call.name.upper()}({value}, {args['n']}) OVER (ORDER BY {order})"
        if call.name == "change":
            return f"{value} - LAG({value}, {args['n']}) OVER (ORDER BY {order})"
        if call.name == "change_pct":
            lag = f"LAG({value}, {args['n']}) OVER (ORDER BY {order})"
            # NULLIF so a zero base reads as "no answer" rather than raising.
            return f"({value} - {lag}) / NULLIF({lag}, 0)"
        if call.name == "time_shift":
            time_dimension = query.time_dimension()
            grain = time_dimension.grain if time_dimension else None
            if time_dimension is None or grain is None:  # pragma: no cover - validate() ran first
                raise QueryError("time_shift needs a time dimension with a grain.")
            periods = args["periods"]
            frame = (
                f"RANGE BETWEEN INTERVAL {periods} {grain.upper()} PRECEDING "
                f"AND INTERVAL {periods} {grain.upper()} PRECEDING"
            )
            # LAST_VALUE over a one-period-wide frame: the value of *that*
            # calendar period, and NULL when the period has no row. LAG would
            # answer with whatever the previous row happens to be.
            return (
                f"LAST_VALUE({value}) OVER "
                f"(ORDER BY {_quote(time_dimension.output_name())} {frame})"
            )
        if call.name in ("rank", "dense_rank", "percent_rank"):
            return f"{call.name.upper()}() OVER (ORDER BY {value} DESC)"
        if call.name == "ntile":
            return f"NTILE({args['buckets']}) OVER (ORDER BY {value} DESC)"
        raise QueryError(f"Transform '{call.name}' has no compiler.")  # pragma: no cover

    @staticmethod
    def _order_expression(query: SemanticQuery, fallback_column: str) -> str:
        """What a sequence window orders by.

        The time dimension when there is one, else the first dimension, else
        the value itself — which only happens when the query has no grouping
        at all and therefore exactly one row.
        """
        time_dimension = query.time_dimension()
        if time_dimension is not None:
            return _quote(time_dimension.output_name())
        if query.dimensions:
            return _quote(query.dimensions[0].output_name())
        return _quote(fallback_column)

    @staticmethod
    def _canonical(sql: str) -> str:
        """Parse as DuckDB and regenerate, proving the SQL is well formed.

        Parsing here rather than trusting the string is what turns a mistake in
        this module into an error at compile time instead of a DuckDB failure
        with no context.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect=DIALECT)
        except Exception as e:
            raise QueryError(f"Generated SQL did not parse: {e}") from e
        if parsed is None:  # pragma: no cover - parse_one raises instead
            raise GuardrailViolation("Generated SQL was empty")
        return parsed.sql(dialect=DIALECT)
