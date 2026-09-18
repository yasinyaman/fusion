"""Core domain models shared by ports, services and adapters.

Everything here is plain Python: no pandas, no Arrow, no DuckDB. Adapters
convert to and from these types at the boundary.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from fusion.domain.errors import SchemaError

MV_PREFIX = "mv_"


@dataclass(frozen=True, slots=True)
class TableRef:
    """A table identified by its source and table name (``source.table``).

    An empty ``source`` denotes an unqualified reference (e.g. a bare table
    name found in SQL that has not been resolved against the catalog yet).
    """

    source: str
    table: str

    @property
    def full_name(self) -> str:
        return f"{self.source}.{self.table}" if self.source else self.table

    @property
    def is_view(self) -> bool:
        """Materialized views live in DuckDB only and are named ``mv_*``."""
        return self.table.startswith(MV_PREFIX)

    @classmethod
    def parse(cls, text: str) -> TableRef:
        """Parse ``'source.table'``; raise SchemaError for any other shape."""
        parts = text.split(".", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise SchemaError(f"Invalid table name '{text}'. Use 'source.table' format.")
        return cls(parts[0], parts[1])

    def __str__(self) -> str:
        return self.full_name


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """A column's name, SQL-ish type name and nullability."""

    name: str
    type: str
    nullable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable}


@dataclass(slots=True)
class TableSchema:
    """Columns, the loaded row count and the source's own size estimate.

    ``row_count`` is how many rows are in the analytics store (``-1`` until
    the table is loaded); ``row_estimate`` is what the source reports about
    the table at the other end (``-1`` when it cannot tell), which is what
    the planner uses before anything is fetched.
    """

    columns: list[ColumnInfo] = field(default_factory=list)
    row_count: int = -1
    row_estimate: int = -1

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    @property
    def known_estimate(self) -> int | None:
        """Best guess at the source-side size, or None when unknown."""
        if self.row_estimate >= 0:
            return self.row_estimate
        return self.row_count if self.row_count >= 0 else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": [c.as_dict() for c in self.columns],
            "row_count": self.row_count,
            "row_estimate": self.row_estimate,
        }


SourceSchema = dict[str, TableSchema]
"""Schema of one source: table name -> TableSchema."""


@dataclass(slots=True)
class RowSet:
    """Columnar-agnostic row transfer type: column names plus row tuples."""

    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]

    @classmethod
    def empty(cls) -> RowSet:
        return cls(columns=(), rows=[])

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> RowSet:
        """Build from dict records; column order is first-seen key order.

        Keys missing from a record become ``None`` (like a DataFrame built
        from a list of dicts).
        """
        materialized = list(records)
        columns: dict[str, None] = {}
        for rec in materialized:
            for key in rec:
                columns.setdefault(key, None)
        names = tuple(columns)
        rows = [tuple(rec.get(name) for name in names) for rec in materialized]
        return cls(columns=names, rows=rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[Any]:
        """Values of one column, in row order."""
        idx = self.columns.index(name)
        return [row[idx] for row in self.rows]

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def head(self, n: int) -> RowSet:
        return RowSet(columns=self.columns, rows=self.rows[:n])


@runtime_checkable
class RowStream(Protocol):
    """Rows arriving in batches, so a large read never sits in memory whole.

    Iterating yields ``RowSet`` batches. ``arrow_reader()`` is the fast path:
    when the adapter already has an Arrow stream (a Warp Arrow IPC export),
    it hands the reader over and the store ingests batches directly, without
    ever building Python tuples. Domain code treats it as an opaque object.
    """

    @property
    def columns(self) -> tuple[str, ...]:
        """Column names (may be empty until the first batch arrives)."""
        ...

    @property
    def schema(self) -> TableSchema | None:
        """Column types when the source announced them, else None."""
        ...

    def __iter__(self) -> Iterator[RowSet]: ...

    def arrow_reader(self) -> Any:
        """A ``pyarrow.RecordBatchReader``-like object, or None. Consumed once."""
        ...

    def close(self) -> None: ...


@dataclass(slots=True)
class ListRowStream:
    """A RowStream over rows already in memory (fakes, small results, tests)."""

    rows: RowSet
    batch_size: int = 10_000
    table_schema: TableSchema | None = None

    @classmethod
    def from_records(
        cls, records: Iterable[Mapping[str, Any]], batch_size: int = 10_000
    ) -> ListRowStream:
        return cls(RowSet.from_records(records), batch_size=batch_size)

    @property
    def columns(self) -> tuple[str, ...]:
        return self.rows.columns

    @property
    def schema(self) -> TableSchema | None:
        return self.table_schema

    def __iter__(self) -> Iterator[RowSet]:
        if not self.rows.rows:
            yield RowSet(columns=self.rows.columns, rows=[])
            return
        for start in range(0, len(self.rows.rows), self.batch_size):
            yield RowSet(
                columns=self.rows.columns,
                rows=self.rows.rows[start : start + self.batch_size],
            )

    def arrow_reader(self) -> Any:
        return None

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """What a data source can do beyond fetching whole tables."""

    #: Runs SQL on its own backend (``PushdownCapable``).
    pushdown: bool = False
    #: Serves filtered/projected reads through ``fetch_slice``.
    slices: bool = False
    #: ``fetch_slice`` can hand over an Arrow stream (no Python row objects).
    arrow: bool = False
    #: Reports table sizes without counting rows.
    row_estimates: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "pushdown": self.pushdown,
            "slices": self.slices,
            "arrow": self.arrow,
            "row_estimates": self.row_estimates,
        }


@dataclass(frozen=True, slots=True)
class TableSize:
    """How much room a table takes in the analytics store."""

    rows: int = 0
    bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"rows": self.rows, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class RefreshSpec:
    """How to refresh one table incrementally.

    ``watermark_column`` is a monotonically increasing column (``updated_at``,
    an id): only rows above the highest value already loaded are fetched.
    ``key_columns`` identify a row, so an updated row replaces its old copy
    instead of being appended twice; without them the new rows are appended.
    """

    watermark_column: str = ""
    key_columns: tuple[str, ...] = ()

    @property
    def is_incremental(self) -> bool:
        return bool(self.watermark_column)

    def as_dict(self) -> dict[str, Any]:
        return {
            "watermark_column": self.watermark_column,
            "key_columns": list(self.key_columns),
        }


@dataclass(slots=True)
class QueryResult:
    """Result of an analytical query with LLM/SDK friendly conversions."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    sql: str = ""
    execution_time_ms: float = 0.0
    from_cache: bool = False

    @classmethod
    def from_rowset(
        cls,
        rowset: RowSet,
        sql: str = "",
        execution_time_ms: float = 0.0,
        from_cache: bool = False,
    ) -> QueryResult:
        return cls(
            columns=list(rowset.columns),
            rows=list(rowset.rows),
            sql=sql,
            execution_time_ms=execution_time_ms,
            from_cache=from_cache,
        )

    def to_rowset(self) -> RowSet:
        return RowSet(columns=tuple(self.columns), rows=list(self.rows))

    def as_cached(self) -> QueryResult:
        """Copy flagged as served from cache (zero execution time)."""
        return replace(self, from_cache=True, execution_time_ms=0.0)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def __len__(self) -> int:
        return self.row_count

    def __repr__(self) -> str:
        return (
            f"QueryResult(rows={self.row_count}, cols={self.column_count}, "
            f"time={self.execution_time_ms:.1f}ms, cached={self.from_cache})"
        )

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_records(), indent=indent, default=str, ensure_ascii=False)

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(self.columns)
        writer.writerows(self.rows)
        return buf.getvalue()

    def to_markdown(self) -> str:
        """GitHub-flavoured pipe table with padded columns."""
        cells = [[_cell(v) for v in row] for row in self.rows]
        widths = [len(c) for c in self.columns]
        for row in cells:
            for i, text in enumerate(row):
                widths[i] = max(widths[i], len(text))
        header = "| " + " | ".join(c.ljust(w) for c, w in zip(self.columns, widths, strict=True))
        separator = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
        lines = [header + " |", separator]
        for row in cells:
            lines.append(
                "| " + " | ".join(t.ljust(w) for t, w in zip(row, widths, strict=True)) + " |"
            )
        return "\n".join(lines)

    def summary(self) -> str:
        lines = [f"Query returned {self.row_count} rows, {self.column_count} columns."]
        lines.append(f"Columns: {', '.join(self.columns)}")
        if self.rows:
            lines.append("First rows:")
            for row in self.rows[:5]:
                lines.append(f"  {row}")
        if self.row_count > 5:
            lines.append(f"  ... and {self.row_count - 5} more rows")
        return "\n".join(lines)


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(slots=True)
class FetchPlan:
    """Which catalog tables a SQL statement needs, plus pushdown eligibility."""

    targets: list[TableRef] = field(default_factory=list)
    strategy_used: str = "sql_parse"
    is_single_source: bool = False
    source_name: str | None = None
    has_mv_reference: bool = False
    all_targets_unloaded: bool = False

    def add(self, ref: TableRef) -> None:
        if ref not in self.targets:
            self.targets.append(ref)

    def is_empty(self) -> bool:
        return not self.targets

    @property
    def pushdown_eligible(self) -> bool:
        """Pushdown needs: >=1 target, one source, no mv_ refs, nothing loaded yet."""
        return (
            not self.is_empty()
            and self.is_single_source
            and not self.has_mv_reference
            and self.all_targets_unloaded
        )


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """A backup on disk: a single ``.duckdb`` file or an EXPORT DATABASE directory."""

    name: str
    path: Path
    kind: Literal["file", "export"]
    size_bytes: int
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 2),
            "created_at": self.created_at.isoformat(),
        }


def coerce_ref(ref: TableRef | str) -> TableRef:
    """Accept a TableRef or a ``'source.table'`` string."""
    return ref if isinstance(ref, TableRef) else TableRef.parse(ref)


def refs_to_names(refs: Sequence[TableRef]) -> list[str]:
    return [r.full_name for r in refs]
