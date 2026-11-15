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
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal

from fusion.domain.models import ColumnInfo, TableRef, TableSchema

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
        if not _orderable(self.value, other.value):
            return False
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


_ISO_TEMPORAL = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")


def _orderable(left: Any, right: Any) -> bool:
    """Whether Python orders these two literals the way a database would.

    Numbers, yes. Strings only when both are zero-padded ISO dates of the same
    shape: ``'9' < '10'`` and ``'2024-2-1' < '2024-10-1'`` are both false as
    text and true as the number or date the column holds. The date/time
    separator must match too: ``'T'`` sorts after ``' '`` whatever the time.
    """
    if isinstance(left, str) and isinstance(right, str):
        return (
            len(left) == len(right)
            and left[10:11] == right[10:11]
            and _ISO_TEMPORAL.fullmatch(left) is not None
            and _ISO_TEMPORAL.fullmatch(right) is not None
        )
    return not isinstance(left, str | bool) and not isinstance(right, str | bool)


_NUMERIC_TYPES = frozenset(
    "integer int int2 int4 int8 bigint smallint tinyint hugeint "
    "double float float4 float8 real decimal numeric".split()
)
_TEXT_TYPES = ("varchar", "text", "char", "string")
_TEMPORAL_TYPES = ("date", "time")


#: Floats a literal compares differently at the source: DuckDB casts ``0.1``
#: to the column's FLOAT, Postgres and MySQL widen the column to double instead.
#: (DuckDB's own ``float`` is single precision; Warp reports doubles as ``double``.)
_APPROXIMATE_TYPES = frozenset({"real", "float", "float4"})
#: Exact numerics: a quoted decimal is cast to the column's scale in DuckDB,
#: so ``'1.005'`` matches 1.01 there and nothing at a source sent ``1.005``.
_EXACT_DECIMAL_TYPES = frozenset({"decimal", "numeric"})


def column_kind(column_type: str | None) -> str | None:
    """``bool``, ``numeric``, ``text`` or ``temporal``; None when unknown.

    Arrays, ranges, maps and structs are none of these: an ``=`` against one
    is not a scalar comparison, so it is unknown and never pushed.
    """
    kind = (column_type or "").lower()
    if any(mark in kind for mark in ("[]", "array", "range", "map(", "struct", "union")):
        return None
    if kind == "list":
        return None
    if kind == "uuid":
        return "uuid"
    words = kind.split("(")[0].split()
    if kind.startswith("bool"):
        return "bool"
    if words and words[0] in _NUMERIC_TYPES:
        return "numeric"
    if kind.startswith(_TEXT_TYPES):
        return "text"
    if kind.startswith(_TEMPORAL_TYPES):
        return "temporal"
    return None


_INTEGER_TYPES = frozenset("integer int int2 int4 int8 bigint smallint tinyint hugeint".split())
_INTEGER_TEXT = re.compile(r"-?[0-9]+")
_DECIMAL_TEXT = re.compile(r"-?([0-9]+\.[0-9]*|\.[0-9]+)")
#: Text operators whose answer cannot lose a row at a source that collates
#: differently: a case-insensitive ``=`` or ``LIKE`` only matches *more* rows,
#: which the local WHERE then drops. ``!=`` and ranges can match fewer.
_TEXT_OPS = frozenset({"eq", "in", "like"})


def _as_column_kind(value: Any, kind: str, column_type: str) -> Any:
    """``value`` as the column's own kind, the way DuckDB casts it; or the sentinel."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)  # DuckDB reads 1 as true; the MySQL tinyint(1) idiom
        return _NO_FORM
    if isinstance(value, bool):
        return _NO_FORM
    if kind == "uuid":
        return value if isinstance(value, str) and _UUID_TEXT.fullmatch(value) else _NO_FORM
    if kind == "numeric":
        base = column_type.lower().split("(")[0].split()[0]
        if isinstance(value, float) and base in _INTEGER_TYPES:
            # A source reading an integer filter refuses 6.5 outright; a whole
            # number goes as the integer it is, and a fraction stays local.
            return int(value) if value.is_integer() else _NO_FORM
        if isinstance(value, int | float):
            return value
        if isinstance(value, str):
            if _INTEGER_TEXT.fullmatch(value):
                return int(value)  # DuckDB casts '042' to 42 for an integer column
            if (
                base not in _INTEGER_TYPES
                and base not in _EXACT_DECIMAL_TYPES
                and _DECIMAL_TEXT.fullmatch(value)
                and len(value.lstrip("-").replace(".", "").lstrip("0")) <= 15
            ):
                return float(value)  # a double column, within a double's digits
        return _NO_FORM
    if kind == "temporal" and isinstance(value, str) and _ZONED.search(column_type.lower()):
        # A zoned timestamp read from text depends on the reader's time zone,
        # which is the source's session zone there and DuckDB's here. Only a
        # literal that names its own offset means the same instant to both.
        return value if _HAS_OFFSET.search(value) else _NO_FORM
    if kind in ("text", "temporal"):
        return value if isinstance(value, str) else _NO_FORM
    return _NO_FORM


_ZONED = re.compile(r"with time zone|timestamptz|tz\b")
#: A time of day followed by its offset: ``10:00+03``, ``10:00:00Z``. A bare
#: date's ``-15`` is not an offset.
_HAS_OFFSET = re.compile(
    r"[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?\s*(Z|[+-][0-9]{2}(:?[0-9]{2})?)$"
)
_UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


_NO_FORM = object()


def pushable_form(predicate: Predicate, column_type: str | None) -> Predicate | None:
    """``predicate`` as a source would evaluate it the way the store does, or None.

    The original WHERE still runs locally, but only over the rows the source
    sent, so a pushed condition may let through more rows than the query keeps
    and never fewer. ``zip = 123`` on a text column matches ``'0123'`` in
    DuckDB (it casts) and not at a source that compares text, so a literal is
    pushed only in its column's own kind: ``id = '42'`` goes as ``id = 42``,
    as DuckDB reads it. On text only ``=``, ``IN`` and ``LIKE`` go (the source's
    collation decides ``!=`` and ranges), and no LIKE pattern with a backslash
    (an escape there, a plain character in DuckDB). Unknown type: not pushed.
    """
    if predicate.op == "is_null":
        return predicate
    kind = column_kind(column_type)
    if kind is None or column_type is None:
        return None
    if kind == "bool" and predicate.op not in ("eq", "ne", "in"):
        return None
    if kind == "numeric" and predicate.op == "like":
        return None
    base = column_type.lower().split("(")[0].split()
    if kind == "numeric" and base and base[0] in _APPROXIMATE_TYPES:
        return None
    if kind == "uuid" and predicate.op not in ("eq", "in"):
        # The source compares uuids case-insensitively, the store as text:
        # = and IN can only match more rows there, != and LIKE fewer.
        return None
    if kind == "text":
        if predicate.op not in _TEXT_OPS:
            return None
        if predicate.op == "like" and "\\" in str(predicate.value):
            return None
    if predicate.op == "in":
        values = tuple(_as_column_kind(v, kind, column_type) for v in predicate.value)
        if any(v is _NO_FORM for v in values):
            return None
        return replace(predicate, value=values)
    value = _as_column_kind(predicate.value, kind, column_type)
    return None if value is _NO_FORM else replace(predicate, value=value)


def merge_same_key(predicates: Sequence[Predicate]) -> tuple[tuple[Predicate, ...], bool]:
    """One condition per column and operator, and whether nothing was lost.

    A source's filters may hold one value per column and operator (Warp's URL
    ones do). Two ``IN`` lists become their intersection; two lower (or upper)
    bounds of one kind keep the tighter one; equal duplicates become one. Any
    other pair keeps its first condition only: the source then sends a
    superset, which the local WHERE narrows, and the second ``bool`` is False
    so the caller knows the source no longer applies everything exactly.
    """
    merged: dict[tuple[str, str], Predicate] = {}
    exact = True
    for p in predicates:
        key = (p.column.lower(), p.op)
        held = merged.get(key)
        if held is None:
            merged[key] = p
        elif held == p:
            continue
        elif p.op == "in":
            wanted = set(p.value)
            both = tuple(v for v in held.value if v in wanted)
            if both:
                merged[key] = replace(held, value=both)
            else:
                exact = False  # no row can match; the local WHERE says so
        elif p.op in (*_LOWER_BOUNDS, *_UPPER_BOUNDS) and _orderable(held.value, p.value):
            lower = p.op in _LOWER_BOUNDS
            tighter = _compare("gt" if lower else "lt", p.value, held.value)
            merged[key] = p if tighter else held
        else:
            exact = False
    return tuple(merged.values()), exact


def ingest_schema(
    announced: TableSchema | None, declared: TableSchema | None
) -> TableSchema | None:
    """The column types a slice is stored with.

    What the stream announced wins (Arrow carries decimals, widths and zones),
    except where it sent text for a column the source declared as a UUID: a
    UUID kept as text compares as text, so ``= 'F4…'`` misses the row the
    source matched case-insensitively and a slice silently loses it.
    Without an announcement, the declared types are used as they are.
    """
    if announced is None or not announced.columns:
        return declared
    if declared is None:
        return announced
    declared_types = {c.name.lower(): c.type for c in declared.columns}
    columns = []
    for column in announced.columns:
        source_type = declared_types.get(column.name.lower())
        if column_kind(column.type) == "text" and column_kind(source_type) == "uuid":
            column = ColumnInfo(column.name, source_type or column.type, column.nullable)
        columns.append(column)
    return TableSchema(columns=columns)


def predicate_is_pushable(predicate: Predicate, column_type: str | None) -> bool:
    """Whether ``predicate`` can be sent to the source at all (see ``pushable_form``)."""
    return pushable_form(predicate, column_type) is not None


def keys_are_pushable(driver_type: str | None, target_type: str | None) -> bool:
    """Whether keys read from one column can be sent as an ``IN`` on another.

    The join compares them in the store, where DuckDB casts (``7 = '07'``);
    the source compares them as they are, so the two kinds must agree.
    """
    kind = column_kind(target_type)
    return kind is not None and kind == column_kind(driver_type)


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
        """True when a slice loaded for ``self`` contains everything ``other`` needs.

        A limit on *this* slice means only some of the matching rows were
        ever loaded, so it can answer nothing but the identical request. A
        limit on the *request* is harmless: taking fewer rows out of a
        complete slice is something the query does locally.
        """
        if self.limit is not None:
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
        """Copy with one more condition (used by semi-joins).

        An ``IN`` on a column that already has one becomes their intersection:
        the same rows, said in one condition a source can take.
        """
        if predicate.op == "in":
            for index, existing in enumerate(self.predicates):
                if existing.op == "in" and existing.column == predicate.column:
                    wanted = set(predicate.value)
                    both = tuple(v for v in existing.value if v in wanted)
                    merged = (*self.predicates[:index], Predicate(predicate.column, "in", both))
                    return SliceSpec(
                        columns=self.columns,
                        predicates=(*merged, *self.predicates[index + 1 :]),
                        limit=self.limit,
                    )
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

    def find_covering(
        self, ref: TableRef, spec: SliceSpec, now: float | None = None
    ) -> LoadedSlice | None:
        """The cheapest loaded slice that covers ``spec``, or None.

        A complete full-table slice wins (it answers anything); otherwise the
        smallest covering slice is chosen so later queries scan as little as
        possible. The winner's ``last_used`` is refreshed for LRU eviction
        when ``now`` is given; a lookup without it leaves the clock alone
        (resetting it to 0 would make the slice the first one evicted).
        """
        candidates = [
            s
            for s in self._slices.values()
            if s.ref == ref and s.reusable and s.spec.subsumes(spec)
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda s: (not s.is_full, s.row_count))
        if now is not None:
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
