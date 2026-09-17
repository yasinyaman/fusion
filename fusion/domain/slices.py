"""Slices: the part of a source table that has been pulled into the store.

A *slice* is a projection (``columns``) plus a conjunction of simple
predicates (``predicates``) and an optional ``limit``. Loading a slice
instead of a whole table is what keeps a 50-million-row source table usable:
only the rows and columns a query actually touches travel over HTTP.

The registry tracks which slices exist, so a later query whose needs are
*contained* in an existing slice reuses it instead of fetching again.
Containment is deliberately conservative: a wrong "yes" would silently drop
rows from a result, so anything not provably covered is refetched.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from fusion.domain.models import TableRef

PredicateOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"]

PREDICATE_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null"}
)

#: Ops that narrow a range in the same direction (used by ``Predicate.implies``).
_LOWER_BOUNDS = ("gt", "gte")
_UPPER_BOUNDS = ("lt", "lte")

_SQL_OP_TEXT = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "in": "IN",
    "is_null": "IS NULL",
}


def _like_to_regex(pattern: str) -> str:
    """Translate a SQL LIKE pattern into an anchored regular expression."""
    out = []
    for char in pattern:
        if char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        else:
            out.append(re.escape(char))
    return "^" + "".join(out) + "$"


@dataclass(frozen=True, slots=True)
class Predicate:
    """One ``column <op> value`` condition.

    ``in`` carries a tuple of values and ``is_null`` a bool (``True`` means
    ``IS NULL``). Values are always literals: a predicate that would depend
    on another column, a function or a bound parameter is never built.
    """

    column: str
    op: PredicateOp = "eq"
    value: Any = None

    def __post_init__(self) -> None:
        if self.op not in PREDICATE_OPS:
            raise ValueError(f"Unknown predicate operator '{self.op}'")
        if self.op == "in":
            if not isinstance(self.value, Iterable) or isinstance(self.value, str | bytes):
                raise ValueError("Predicate 'in' needs a sequence of values")
            object.__setattr__(self, "value", tuple(self.value))
        elif self.op == "is_null":
            object.__setattr__(self, "value", bool(self.value))

    # -- evaluation ---------------------------------------------------------

    def matches(self, row: Mapping[str, Any]) -> bool:
        """Whether ``row`` satisfies this predicate (NULL never compares true)."""
        actual = row.get(self.column)
        if self.op == "is_null":
            return (actual is None) is bool(self.value)
        if actual is None:
            return False
        if self.op == "in":
            return actual in self.value
        if self.op == "like":
            return re.match(_like_to_regex(str(self.value)), str(actual)) is not None
        if self.op == "eq":
            return bool(actual == self.value)
        if self.op == "ne":
            return bool(actual != self.value)
        return _compare(self.op, actual, self.value)

    # -- containment --------------------------------------------------------

    def implies(self, other: Predicate) -> bool:
        """True when every row satisfying ``self`` also satisfies ``other``.

        Conservative on purpose: equality, ``eq`` inside an ``in`` list or a
        range, a narrower bound in the same direction, and a subset ``in``.
        Everything else answers False, which only costs a refetch.
        """
        if self.column != other.column:
            return False
        if self == other:
            return True
        if other.op == "in":
            if self.op == "eq":
                return self.value in other.value
            if self.op == "in":
                return set(self.value) <= set(other.value)
            return False
        if other.op == "eq":
            return self.op == "in" and set(self.value) == {other.value}
        if other.op in _LOWER_BOUNDS and self.op in (*_LOWER_BOUNDS, "eq"):
            strict = other.op == "gt" and self.op != "gt"
            return _compare("gt" if strict else "gte", self.value, other.value)
        if other.op in _UPPER_BOUNDS and self.op in (*_UPPER_BOUNDS, "eq"):
            strict = other.op == "lt" and self.op != "lt"
            return _compare("lt" if strict else "lte", self.value, other.value)
        return False

    def as_dict(self) -> dict[str, Any]:
        return {"column": self.column, "op": self.op, "value": _plain(self.value)}

    def __str__(self) -> str:
        if self.op == "is_null":
            return f"{self.column} IS {'' if self.value else 'NOT '}NULL"
        return f"{self.column} {_SQL_OP_TEXT[self.op]} {self.value!r}"


def _compare(op: str, left: Any, right: Any) -> bool:
    """Ordering comparison that answers False for values that cannot be compared."""
    try:
        if op == "gt":
            return bool(left > right)
        if op == "gte":
            return bool(left >= right)
        if op == "lt":
            return bool(left < right)
        if op == "lte":
            return bool(left <= right)
    except TypeError:
        return False
    return False


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


@dataclass(frozen=True, slots=True)
class SliceSpec:
    """Which columns and rows of a table are wanted.

    ``columns = None`` means every column. ``predicates`` is an AND; an empty
    tuple means every row. ``limit`` marks a partial read: such a slice is
    only ever reused for the exact same spec, since which rows it holds is
    not defined by the predicates alone.
    """

    columns: frozenset[str] | None = None
    predicates: tuple[Predicate, ...] = ()
    limit: int | None = None

    FULL: ClassVar[SliceSpec]

    @property
    def is_full(self) -> bool:
        """The whole table: all columns, no filter, no limit."""
        return self.columns is None and not self.predicates and self.limit is None

    def normalized(self) -> SliceSpec:
        """Same slice with predicates in a canonical order (for hashing/equality)."""
        return SliceSpec(
            columns=self.columns,
            predicates=tuple(
                sorted(self.predicates, key=lambda p: (p.column, p.op, repr(p.value)))
            ),
            limit=self.limit,
        )

    def subsumes(self, other: SliceSpec) -> bool:
        """True when a slice loaded for ``self`` contains everything ``other`` needs."""
        if self.limit is not None or other.limit is not None:
            return self.normalized() == other.normalized()
        if self.columns is not None and (
            other.columns is None or not other.columns <= self.columns
        ):
            return False
        return all(
            any(theirs.implies(mine) for theirs in other.predicates) for mine in self.predicates
        )

    def table_suffix(self) -> str:
        """Short, stable identifier of this spec (used in the slice table name)."""
        if self.is_full:
            return ""
        payload = repr(self.normalized())
        return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]

    def table_name_for(self, ref: TableRef) -> str:
        """Where a slice of ``ref`` with this spec lives in the store."""
        suffix = self.table_suffix()
        return ref.full_name if not suffix else f"{ref.full_name}__s_{suffix}"

    def with_predicate(self, predicate: Predicate) -> SliceSpec:
        """Copy with one more condition (used by semi-joins)."""
        return SliceSpec(
            columns=self.columns, predicates=(*self.predicates, predicate), limit=self.limit
        )

    def matches(self, row: Mapping[str, Any]) -> bool:
        """Whether ``row`` satisfies every predicate (ignores columns/limit)."""
        return all(p.matches(row) for p in self.predicates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": None if self.columns is None else sorted(self.columns),
            "predicates": [p.as_dict() for p in self.predicates],
            "limit": self.limit,
        }

    def describe(self) -> str:
        """One-line human description, e.g. ``columns=id,name where status = 'x'``."""
        parts = []
        if self.columns is not None:
            parts.append("columns=" + ",".join(sorted(self.columns)))
        if self.predicates:
            parts.append("where " + " AND ".join(str(p) for p in self.predicates))
        if self.limit is not None:
            parts.append(f"limit {self.limit}")
        return " ".join(parts) or "full table"


SliceSpec.FULL = SliceSpec()


@dataclass(slots=True)
class LoadedSlice:
    """A slice that exists in the analytics store right now."""

    ref: TableRef
    spec: SliceSpec
    table_name: str
    row_count: int = 0
    loaded_at: float = 0.0
    last_used: float = 0.0
    #: False when the load stopped at a row cap, so rows may be missing.
    complete: bool = True
    #: Set for slices produced by a semi-join (``semijoin:driver.key``).
    derived_from: str | None = None
    #: Highest watermark value seen, for incremental refresh.
    watermark: Any = None

    @property
    def is_full(self) -> bool:
        return self.spec.is_full

    @property
    def reusable(self) -> bool:
        """Only a complete slice may serve a *different* spec."""
        return self.complete

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table_name,
            "spec": self.spec.as_dict(),
            "description": self.spec.describe(),
            "row_count": self.row_count,
            "complete": self.complete,
            "derived_from": self.derived_from,
        }


class SliceRegistry:
    """Which slices of which tables are materialized, keyed by store table name."""

    def __init__(self) -> None:
        self._slices: dict[str, LoadedSlice] = {}

    def __len__(self) -> int:
        return len(self._slices)

    def __contains__(self, table_name: object) -> bool:
        return table_name in self._slices

    def record(self, loaded: LoadedSlice) -> LoadedSlice:
        """Register (or replace) a slice; returns it."""
        self._slices[loaded.table_name] = loaded
        return loaded

    def get(self, table_name: str) -> LoadedSlice | None:
        return self._slices.get(table_name)

    def slices(self, ref: TableRef) -> list[LoadedSlice]:
        """Every slice of one table, newest first."""
        found = [s for s in self._slices.values() if s.ref == ref]
        found.sort(key=lambda s: s.loaded_at, reverse=True)
        return found

    def all(self) -> list[LoadedSlice]:
        return list(self._slices.values())

    def full_slice(self, ref: TableRef) -> LoadedSlice | None:
        """The whole-table slice of ``ref``, when it is loaded and complete."""
        for loaded in self._slices.values():
            if loaded.ref == ref and loaded.is_full and loaded.complete:
                return loaded
        return None

    def find_covering(self, ref: TableRef, spec: SliceSpec, now: float = 0.0) -> LoadedSlice | None:
        """The cheapest loaded slice that covers ``spec``, or None.

        A complete full-table slice wins (it answers anything); otherwise the
        smallest covering slice is chosen so later queries scan as little as
        possible. The winner's ``last_used`` is refreshed for LRU eviction.
        """
        candidates = [
            s
            for s in self._slices.values()
            if s.ref == ref and s.reusable and s.spec.subsumes(spec)
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda s: (not s.is_full, s.row_count))
        best.last_used = now
        return best

    def touch(self, table_name: str, now: float) -> None:
        loaded = self._slices.get(table_name)
        if loaded is not None:
            loaded.last_used = now

    def evict(self, table_name: str) -> LoadedSlice | None:
        """Forget one slice; returns it when it was registered."""
        return self._slices.pop(table_name, None)

    def evict_ref(self, ref: TableRef) -> list[LoadedSlice]:
        """Forget every slice of one table."""
        dropped = [s for s in self._slices.values() if s.ref == ref]
        for loaded in dropped:
            del self._slices[loaded.table_name]
        return dropped

    def evict_source(self, source: str) -> list[LoadedSlice]:
        """Forget every slice belonging to one source."""
        dropped = [s for s in self._slices.values() if s.ref.source == source]
        for loaded in dropped:
            del self._slices[loaded.table_name]
        return dropped

    def total_rows(self) -> int:
        return sum(s.row_count for s in self._slices.values())

    def lru_candidates(self, protect: Iterable[str] = ()) -> list[LoadedSlice]:
        """Eviction order: least recently used first, ``protect``ed names excluded."""
        keep = set(protect)
        return sorted(
            (s for s in self._slices.values() if s.table_name not in keep),
            key=lambda s: (s.last_used, s.loaded_at),
        )

    def __iter__(self) -> Iterator[LoadedSlice]:
        return iter(self._slices.values())


def columns_of(columns: Sequence[str] | Iterable[str] | None) -> frozenset[str] | None:
    """Normalize a column selection to a frozenset (``None`` stays ``None``)."""
    if columns is None:
        return None
    selected = frozenset(columns)
    return selected or None
