"""What a SQL statement asks of each table it reads.

``QueryShape`` is the analyzer's answer to "which columns and which rows
does this query actually need?". The planner turns it into slices. Anything
the analyzer cannot reason about safely (CTEs, set operations, subqueries,
window functions) comes back with ``is_simple_select=False``, and the
planner then loads whole tables — the 1.0 behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fusion.domain.models import TableRef
from fusion.domain.slices import Predicate, SliceSpec


@dataclass(frozen=True, slots=True)
class TableUse:
    """How one table is used by a query.

    ``columns = None`` means "every column" (a star, or a column the analyzer
    could not attribute). ``predicates`` are the WHERE conditions that apply
    to this table alone and may therefore be pushed to the source.
    ``outer_null_side`` marks a table whose rows may be NULL-extended by an
    outer join; filtering it at the source would change the result, so no
    predicate is ever recorded for it.
    """

    ref: TableRef
    alias: str = ""
    columns: frozenset[str] | None = None
    predicates: tuple[Predicate, ...] = ()
    outer_null_side: bool = False

    def slice_spec(self, limit: int | None = None) -> SliceSpec:
        """The slice this usage needs (``limit`` only when the caller proved it safe)."""
        return SliceSpec(columns=self.columns, predicates=self.predicates, limit=limit)

    @property
    def is_unrestricted(self) -> bool:
        return self.columns is None and not self.predicates

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.ref.full_name,
            "alias": self.alias,
            "columns": None if self.columns is None else sorted(self.columns),
            "predicates": [p.as_dict() for p in self.predicates],
            "outer_null_side": self.outer_null_side,
        }


@dataclass(frozen=True, slots=True)
class JoinEquality:
    """An ``a.x = b.y`` join condition between two aliases."""

    left_alias: str
    left_column: str
    right_alias: str
    right_column: str
    inner: bool = True

    def other_side(self, alias: str) -> tuple[str, str] | None:
        """``(alias, column)`` of the side that is not ``alias``, plus our column."""
        if alias == self.left_alias:
            return self.right_alias, self.right_column
        if alias == self.right_alias:
            return self.left_alias, self.left_column
        return None

    def column_for(self, alias: str) -> str | None:
        if alias == self.left_alias:
            return self.left_column
        if alias == self.right_alias:
            return self.right_column
        return None


@dataclass(frozen=True, slots=True)
class QueryShape:
    """Per-table needs of one statement, as far as they can be trusted."""

    tables: tuple[TableUse, ...] = ()
    joins: tuple[JoinEquality, ...] = ()
    limit: int | None = None
    #: False for CTEs, set operations, subqueries and window functions.
    is_simple_select: bool = True
    #: True when the statement sorts or groups: a LIMIT then depends on rows
    #: the source would have to send anyway, so it must not be pushed down.
    is_ordered: bool = False
    is_aggregated: bool = False

    @property
    def limit_is_pushable(self) -> bool:
        """Whether ``limit`` may be applied while reading a single source table."""
        return (
            self.limit is not None
            and self.single_table
            and self.is_simple_select
            and not self.is_ordered
            and not self.is_aggregated
        )

    def use_for(self, ref: TableRef) -> TableUse | None:
        for use in self.tables:
            if use.ref == ref:
                return use
        return None

    def use_for_alias(self, alias: str) -> TableUse | None:
        for use in self.tables:
            if use.alias == alias:
                return use
        return None

    @property
    def single_table(self) -> bool:
        return len(self.tables) == 1 and not self.joins

    def inner_joins_for(self, alias: str) -> tuple[JoinEquality, ...]:
        """Inner-join equalities touching ``alias`` (safe for key passing)."""
        return tuple(j for j in self.joins if j.inner and j.column_for(alias) is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_simple_select": self.is_simple_select,
            "limit": self.limit,
            "is_ordered": self.is_ordered,
            "is_aggregated": self.is_aggregated,
            "tables": [t.as_dict() for t in self.tables],
            "joins": [
                {
                    "left": f"{j.left_alias}.{j.left_column}",
                    "right": f"{j.right_alias}.{j.right_column}",
                    "inner": j.inner,
                }
                for j in self.joins
            ],
        }


UNKNOWN_SHAPE = QueryShape(is_simple_select=False)
"""Shape for SQL the analyzer will not reason about (load whole tables)."""
