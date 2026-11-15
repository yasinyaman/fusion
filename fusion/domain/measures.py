"""The semantic model: what a table's numbers and breakdowns are called.

A :class:`SemanticModel` names the *measures* (numbers worth aggregating) and
*dimensions* (ways to break them down) of one table. Nothing here holds SQL: a
measure is a column plus an aggregation, so every part of a model is an
identifier that can be checked against the catalog before it reaches a query.

``infer_model`` builds a usable model from the schema Fusion already discovered,
so ``list_metrics`` answers for any connected table with no configuration.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Literal

from fusion.domain.errors import QueryError
from fusion.domain.identifiers import ALLOWED_AGG_FUNCS
from fusion.domain.models import TableRef, TableSchema

#: Aggregations a metric expression may use.
#:
#: A superset of ``ALLOWED_AGG_FUNCS``, which stays exactly as it is: that set
#: is the published ``aggregate_data`` tool contract, and its members are
#: interpolated as ``f"{func}({column})"``. Two names here are not SQL
#: functions at all — ``COUNT_DISTINCT`` compiles to ``COUNT(DISTINCT x)`` and
#: ``WEIGHTED_AVG`` to ``SUM(x * w) / NULLIF(SUM(w), 0)`` — so they could never
#: belong to that set.
SEMANTIC_AGGS: frozenset[str] = ALLOWED_AGG_FUNCS | {"COUNT_DISTINCT", "MEDIAN", "WEIGHTED_AVG"}

#: Aggregations that need a numeric column.
NUMERIC_AGGS: frozenset[str] = frozenset({"SUM", "AVG", "MEDIAN", "WEIGHTED_AVG"})

#: The measure name that means "rows", so ``*:count`` has something to bind to.
ROW_MEASURE = "*"

TimeGrain = Literal["day", "week", "month", "quarter", "year"]
TIME_GRAINS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")

_NUMERIC_HINTS = ("int", "float", "double", "decimal", "numeric", "real", "bigint", "hugeint")
_TEMPORAL_HINTS = ("date", "timestamp", "time")


def _is_numeric(column_type: str) -> bool:
    lowered = column_type.lower()
    return any(hint in lowered for hint in _NUMERIC_HINTS) and "interval" not in lowered


def _is_temporal(column_type: str) -> bool:
    lowered = column_type.lower()
    return any(lowered.startswith(hint) for hint in _TEMPORAL_HINTS)


def normalize_agg(agg: str, measure: str) -> str:
    """Upper-case an aggregation name and check it is one we support.

    Raises:
        QueryError: Listing every allowed aggregation, lower-cased to match
            how they are written in a metric expression.
    """
    upper = agg.upper()
    if upper in SEMANTIC_AGGS:
        return upper
    allowed = ", ".join(sorted(name.lower() for name in SEMANTIC_AGGS))
    message = f"Unknown aggregation '{agg}' for measure '{measure}'."
    close = difflib.get_close_matches(upper, sorted(SEMANTIC_AGGS), n=1, cutoff=0.6)
    if close:
        message += f" Did you mean '{close[0].lower()}'?"
    raise QueryError(f"{message} Allowed: {allowed}.")


#: Aggregations that take a keyword argument, and the argument they need.
#: The value names a column, so it is checked against the catalog later.
AGG_ARGUMENTS: dict[str, str] = {"WEIGHTED_AVG": "weight"}


def check_agg_args(agg: str, args: tuple[tuple[str, Any], ...], measure: str) -> dict[str, str]:
    """Validate the keyword arguments of an aggregation.

    Args:
        agg: Already normalized (upper-case) aggregation name.
        args: ``(name, value)`` pairs as written in the expression.
        measure: The measure being aggregated, for the error message.

    Returns:
        The bound arguments, values coerced to column names.

    Raises:
        QueryError: On an unknown, missing or non-identifier argument.
    """
    required = AGG_ARGUMENTS.get(agg)
    supplied = dict(args)
    lowered = agg.lower()
    for name in supplied:
        if name != required:
            accepted = required or "none"
            raise QueryError(f"{lowered} has no argument '{name}'. Accepted arguments: {accepted}.")
    if required is None:
        return {}
    if required not in supplied:
        raise QueryError(
            f"{lowered} needs a '{required}' argument naming a column, "
            f"e.g. {measure}:{lowered}({required}=quantity)."
        )
    value = supplied[required]
    if not isinstance(value, str):
        raise QueryError(
            f"{lowered}'s '{required}' must name a column, got {value!r}. "
            f"For example: {measure}:{lowered}({required}=quantity)."
        )
    return {required: value}


@dataclass(frozen=True, slots=True)
class Measure:
    """A number worth aggregating: one column, plus the default way to do it."""

    name: str
    column: str
    default_agg: str = "SUM"
    numeric: bool = True
    description: str = ""

    @property
    def is_row_count(self) -> bool:
        """Whether this is the synthetic "rows" measure behind ``*:count``."""
        return self.column == ROW_MEASURE

    def check_agg(self, agg: str) -> str:
        """Validate an aggregation against this measure's column type.

        Raises:
            QueryError: When the aggregation needs a number and the column is
                not one, or when a non-counting aggregation is asked of rows.
        """
        upper = normalize_agg(agg, self.name)
        if self.is_row_count and upper not in ("COUNT", "COUNT_DISTINCT"):
            raise QueryError(
                f"'{ROW_MEASURE}' counts rows, so it only takes count "
                f"(got '{agg.lower()}'). Use '{ROW_MEASURE}:count'."
            )
        if upper in NUMERIC_AGGS and not self.numeric:
            raise QueryError(
                f"'{upper.lower()}' needs a numeric column, but measure "
                f"'{self.name}' is {self.column} which is not numeric. "
                f"Use count, count_distinct, min or max instead."
            )
        return upper

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "column": self.column,
            "default_aggregation": self.default_agg.lower(),
            "numeric": self.numeric,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Dimension:
    """A way to break measures down: one column, optionally a time column."""

    name: str
    column: str
    temporal: bool = False
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "column": self.column,
            "temporal": self.temporal,
            "grains": list(TIME_GRAINS) if self.temporal else [],
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SemanticModel:
    """The measures and dimensions of one table."""

    ref: TableRef
    measures: tuple[Measure, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    source: str = "inferred"
    _by_measure: dict[str, Measure] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _by_dimension: dict[str, Dimension] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_measure", {m.name: m for m in self.measures})
        object.__setattr__(self, "_by_dimension", {d.name: d for d in self.dimensions})

    @property
    def measure_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.measures)

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(d.name for d in self.dimensions)

    def measure(self, name: str) -> Measure:
        """Look up a measure.

        Raises:
            QueryError: Naming the nearest match and pointing at ``list_metrics``.
        """
        found = self._by_measure.get(name)
        if found is not None:
            return found
        message = f"Unknown measure '{name}' for {self.ref.full_name}."
        close = difflib.get_close_matches(name, self.measure_names, n=1, cutoff=0.6)
        if close:
            message += f" Did you mean '{close[0]}'?"
        available = ", ".join(self.measure_names) or "none"
        raise QueryError(
            f"{message} Available measures: {available}. "
            f"Call list_metrics('{self.ref.full_name}') for the full model."
        )

    def dimension(self, name: str) -> Dimension:
        """Look up a dimension.

        Raises:
            QueryError: Naming the nearest match and pointing at ``list_metrics``.
        """
        found = self._by_dimension.get(name)
        if found is not None:
            return found
        message = f"Unknown dimension '{name}' for {self.ref.full_name}."
        close = difflib.get_close_matches(name, self.dimension_names, n=1, cutoff=0.6)
        if close:
            message += f" Did you mean '{close[0]}'?"
        available = ", ".join(self.dimension_names) or "none"
        raise QueryError(
            f"{message} Available dimensions: {available}. "
            f"Call list_metrics('{self.ref.full_name}') for the full model."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.ref.full_name,
            "source": self.source,
            "measures": [m.as_dict() for m in self.measures],
            "dimensions": [d.as_dict() for d in self.dimensions],
        }


def infer_model(ref: TableRef, schema: TableSchema) -> SemanticModel:
    """Build a model from a discovered table schema.

    Numeric columns become measures defaulting to ``SUM``, everything else
    becomes a dimension, and temporal columns are marked so they can carry a
    grain. Every column is also available as a dimension, because grouping by
    an amount is unusual but not wrong, and refusing it would be surprising.
    """
    measures = [
        Measure(name=ROW_MEASURE, column=ROW_MEASURE, default_agg="COUNT", description="Row count")
    ]
    dimensions: list[Dimension] = []
    for column in schema.columns:
        if _is_numeric(column.type):
            measures.append(Measure(name=column.name, column=column.name, default_agg="SUM"))
            dimensions.append(Dimension(name=column.name, column=column.name))
            continue
        temporal = _is_temporal(column.type)
        dimensions.append(Dimension(name=column.name, column=column.name, temporal=temporal))
        # A non-numeric column can still be counted or bounded.
        measures.append(
            Measure(
                name=column.name, column=column.name, default_agg="COUNT_DISTINCT", numeric=False
            )
        )
    return SemanticModel(
        ref=ref, measures=tuple(measures), dimensions=tuple(dimensions), source="inferred"
    )
