"""A metric question, and the shape of the scan that answers it.

``SemanticQuery`` is what the DSL means once it has been resolved against a
model: which measures, broken down by which dimensions, over which rows.

Because it is built rather than parsed, it knows exactly which columns and
predicates the eventual SQL will use — so :meth:`SemanticQuery.query_shape`
returns an *exact* ``QueryShape`` and the planner can slice a table that the
SQL analyzer would have refused to reason about (a window function or a CTE
makes ``SqlglotAnalyzer.analyze`` give up, and the planner then loads whole
tables). That is the whole point: the transforms this layer exists to offer
are exactly the ones that blind the analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fusion.domain.errors import QueryError
from fusion.domain.identifiers import MAX_RESULT_ROWS, validate_identifier
from fusion.domain.measures import TIME_GRAINS, Dimension, SemanticModel, check_agg_args
from fusion.domain.metric_dsl import MetricExpr, measure_ref_of, needs_order, needs_time
from fusion.domain.models import TableSchema
from fusion.domain.query_shape import QueryShape, TableUse
from fusion.domain.slices import Predicate

#: The alias the compiler gives the fact table.
FACT_ALIAS = "f"


@dataclass(frozen=True, slots=True)
class DimensionRef:
    """A dimension in a query, with a grain when it is a time column."""

    dimension: Dimension
    grain: str | None = None

    @property
    def column(self) -> str:
        return self.dimension.column

    def output_name(self) -> str:
        return self.dimension.name

    def as_text(self) -> str:
        return f"{self.dimension.name}:{self.grain}" if self.grain else self.dimension.name


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """SQL for one semantic query, plus the flat scan underneath it.

    ``base_sql`` is the inner ``SELECT`` on its own: the part that reads the
    table, before any window is layered on. It is what ``explain`` shows, and
    what the contract tests hand to the SQL analyzer to check the shape this
    query claimed.
    """

    sql: str
    base_sql: str
    columns: tuple[str, ...]


def parse_dimension(text: str, model: SemanticModel) -> DimensionRef:
    """Resolve ``order_date:month`` (or plain ``status``) against a model.

    Raises:
        QueryError: On an unknown dimension, an unknown grain, or a grain on a
            column that does not hold a time.
    """
    if not isinstance(text, str) or not text.strip():
        raise QueryError("A dimension cannot be empty. Name a column, e.g. 'status'.")
    name, _, grain = text.strip().partition(":")
    dimension = model.dimension(name)
    if not grain:
        return DimensionRef(dimension=dimension)
    if not dimension.temporal:
        raise QueryError(
            f"'{name}' is not a time column, so it takes no grain. "
            f"Drop the ':{grain}' and group by '{name}' itself."
        )
    if grain not in TIME_GRAINS:
        raise QueryError(
            f"Unknown time grain '{grain}' for '{name}'. Available: {', '.join(TIME_GRAINS)}."
        )
    return DimensionRef(dimension=dimension, grain=grain)


@dataclass(frozen=True, slots=True)
class SemanticQuery:
    """Measures, broken down by dimensions, over filtered rows."""

    model: SemanticModel
    metrics: tuple[MetricExpr, ...]
    dimensions: tuple[DimensionRef, ...] = ()
    filters: tuple[Predicate, ...] = ()
    order_by: str = ""
    descending: bool = False
    limit: int = MAX_RESULT_ROWS

    # -- what the query touches ---------------------------------------------

    def required_columns(self) -> frozenset[str]:
        """Every source column the compiled SQL will read.

        The slice loaded for this query is built from this set, so anything
        missing here would be missing from the store when the SQL runs.
        """
        columns: set[str] = set()
        for expr in self.metrics:
            ref = measure_ref_of(expr)
            measure = self.model.measure(ref.measure)
            if not measure.is_row_count:
                columns.add(measure.column)
            # weighted_avg(weight=quantity) reads a second column.
            columns.update(check_agg_args(ref.agg, ref.args, ref.measure).values())
        columns.update(dim.column for dim in self.dimensions)
        columns.update(predicate.column for predicate in self.filters)
        return frozenset(columns)

    def output_columns(self) -> tuple[str, ...]:
        """The result columns, dimensions first, in the order they appear."""
        return tuple(
            [dim.output_name() for dim in self.dimensions]
            + [expr.output_name() for expr in self.metrics]
        )

    def time_dimension(self) -> DimensionRef | None:
        """The first dimension carrying a grain, if any."""
        for dim in self.dimensions:
            if dim.grain:
                return dim
        return None

    @property
    def needs_calendar(self) -> bool:
        return any(needs_time(expr) for expr in self.metrics)

    @property
    def is_windowed(self) -> bool:
        return any(needs_order(expr) for expr in self.metrics)

    def order_column(self) -> tuple[str, bool]:
        """``(column, descending)`` for the final ORDER BY.

        Defaults to the first dimension ascending, or the first metric
        descending when there is nothing to group by — which is what "top N"
        means and what a caller almost always wants.
        """
        available = self.output_columns()
        if self.order_by:
            requested = self.order_by
            descending = self.descending
            if requested.startswith("-"):
                requested, descending = requested[1:], True
            if requested not in available:
                raise QueryError(
                    f"Cannot order by '{requested}': it is not one of this query's "
                    f"columns ({', '.join(available)})."
                )
            return requested, descending
        if self.dimensions:
            return self.dimensions[0].output_name(), self.descending
        return self.metrics[0].output_name(), True

    # -- validation ---------------------------------------------------------

    def validate(self, schema: TableSchema | None = None) -> None:
        """Check the query against the model and, when known, the real schema.

        Every column reaching SQL is therefore both a syntactically safe
        identifier and one the table actually has — an LLM cannot name a
        column into existence.

        Raises:
            QueryError: On an empty metric list, a bad identifier, a column the
                table does not have, a limit out of range, or a calendar
                transform with no time dimension to shift along.
        """
        if not self.metrics:
            raise QueryError(
                "Ask for at least one metric, e.g. metrics=['revenue:sum'] — "
                "call list_metrics to see what this table offers."
            )
        for expr in self.metrics:
            ref = measure_ref_of(expr)
            self.model.measure(ref.measure).check_agg(ref.agg)
        if self.needs_calendar and self.time_dimension() is None:
            raise QueryError(
                "time_shift needs a time dimension with a grain. Add one, e.g. "
                "dimensions=['order_date:month'], or use lag(...) to shift by "
                "result rows instead of by calendar periods."
            )
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1:
            raise QueryError(f"limit must be a positive whole number, got {self.limit!r}.")
        self.order_column()

        columns = self.required_columns()
        for column in sorted(columns):
            validate_identifier(column, "column name")
        if schema is None:
            return
        known = set(schema.column_names)
        missing = sorted(columns - known)
        if missing:
            raise QueryError(
                f"{self.model.ref.full_name} has no column "
                f"{', '.join(repr(m) for m in missing)}. "
                f"Available columns: {', '.join(sorted(known))}."
            )

    # -- what the planner needs ---------------------------------------------

    def query_shape(self) -> QueryShape:
        """The exact shape of the scan underneath this query.

        ``limit`` is deliberately ``None``: the DSL's limit caps the *result*
        rows, which exist only after grouping, so it can never cap the rows
        read from the source. ``is_aggregated`` and ``is_ordered`` say the same
        thing a second and third way, and each alone is enough to stop
        ``limit_is_pushable`` returning True.
        """
        use = TableUse(
            ref=self.model.ref,
            alias=FACT_ALIAS,
            columns=self.required_columns() or None,
            predicates=self.filters,
        )
        return QueryShape(
            tables=(use,),
            joins=(),
            limit=None,
            is_simple_select=True,
            is_ordered=True,
            is_aggregated=True,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.model.ref.full_name,
            "metrics": [expr.as_text() for expr in self.metrics],
            "dimensions": [dim.as_text() for dim in self.dimensions],
            "filters": [predicate.as_dict() for predicate in self.filters],
            "order_by": self.order_column()[0],
            "descending": self.order_column()[1],
            "limit": self.limit,
        }
