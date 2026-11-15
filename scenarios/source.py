"""An in-process DataSource over a synthesized database.

It stands where Warp stands in production: it filters and projects on its own
side, hands rows over as an Arrow stream, estimates sizes before anything is
fetched, and (when the database allows raw SQL) runs pushed-down queries. The
stream is Warp's own ``ArrowRowStream``, so the store ingests exactly as it
does behind a real gateway.

Everything above the port is the real engine; only the wire is missing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import duckdb

from fusion.adapters.outbound.warp.streams import ArrowRowStream
from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.models import (
    ColumnInfo,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
    TableSchema,
)
from fusion.domain.slices import Predicate, SliceSpec

_OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def predicate_sql(predicate: Predicate) -> str:
    column = quote(predicate.column)
    if predicate.op == "is_null":
        return f"{column} IS {'' if predicate.value else 'NOT '}NULL"
    if predicate.op == "in":
        if not predicate.value:
            return "FALSE"
        return f"{column} IN ({', '.join(literal(v) for v in predicate.value)})"
    if predicate.op == "like":
        return f"{column} LIKE {literal(predicate.value)}"
    return f"{column} {_OPS[predicate.op]} {literal(predicate.value)}"


@dataclass(slots=True)
class SourceStats:
    """What crossed the port, for the report."""

    slice_fetches: int = 0
    full_fetches: int = 0
    estimates: int = 0
    pushdowns: int = 0
    rows_sent: int = 0
    log: list[str] = field(default_factory=list)


class _CountingReader:
    """An Arrow reader that counts the rows it hands over."""

    def __init__(self, reader: Any, stats: SourceStats, lock: threading.Lock) -> None:
        self._reader = reader
        self._stats = stats
        self._lock = lock
        self.schema = reader.schema

    def __iter__(self) -> Any:
        for batch in self._reader:
            with self._lock:
                self._stats.rows_sent += batch.num_rows
            yield batch


class DuckSource:
    """Serves one attached catalog of the world through the DataSource port."""

    source_type = "synthetic"

    def __init__(
        self,
        name: str,
        conn: duckdb.DuckDBPyConnection,
        catalog: str,
        *,
        pushdown: bool = False,
        batch_rows: int = 65_536,
        latency_ms: float = 0.0,
        export_max_rows: int | None = None,
    ) -> None:
        self.name = name
        self._conn = conn
        self._catalog = catalog
        self._pushdown = pushdown
        self._batch_rows = batch_rows
        self._latency = latency_ms / 1000.0
        #: The source's own ceiling on a single read (Warp's ``export.max_rows``).
        self.export_max_rows = export_max_rows
        self._columns: dict[str, list[str]] = {}
        self._connected = False
        self._lock = threading.Lock()
        self.stats = SourceStats()
        #: Set to make every call fail, as a gateway that went away would.
        self.down = False

    # -- helpers ------------------------------------------------------------

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        if self.down:
            raise ConnectionError(f"synthetic source {self.name} is unreachable")
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")
        if self._latency:
            time.sleep(self._latency)
        cursor = self._conn.cursor()
        cursor.execute("SET TimeZone = 'UTC'")
        return cursor

    def _qualified(self, table: str) -> str:
        if table not in self._columns:
            raise QueryError(f"Failed to fetch data from {table}: unknown table")
        return f"{quote(self._catalog)}.main.{quote(table)}"

    def _note(self, message: str) -> None:
        with self._lock:
            self.stats.log.append(message)
            del self.stats.log[:-200]

    def _select(self, table: str, spec: SliceSpec, max_rows: int | None) -> str:
        known = self._columns[table]
        if spec.columns is None:
            columns = "*"
        else:
            unknown = sorted(c for c in spec.columns if c not in known)
            if unknown:
                # Warp answers 400 to a field the table does not have; so does this.
                raise QueryError(
                    f"HTTP 400: unknown field(s) {', '.join(unknown)} for table {table}"
                )
            columns = ", ".join(quote(c) for c in known if c in spec.columns) or "*"
        sql = f"SELECT {columns} FROM {self._qualified(table)}"
        if spec.predicates:
            sql += " WHERE " + " AND ".join(predicate_sql(p) for p in spec.predicates)
        caps = [c for c in (spec.limit, max_rows, self.export_max_rows) if c is not None]
        if caps:
            sql += f" LIMIT {min(caps)}"
        return sql

    # -- DataSource ---------------------------------------------------------

    def connect(self) -> None:
        if self.down:
            raise ConnectionError(f"synthetic source {self.name} is unreachable")
        self._connected = True
        self.discover_schema()

    def close(self) -> None:
        self._connected = False

    def discover_schema(self) -> SourceSchema:
        cursor = self._cursor()
        rows = cursor.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_catalog = ? AND table_schema = 'main' "
            "ORDER BY table_name, ordinal_position",
            [self._catalog],
        ).fetchall()
        by_table: dict[str, list[ColumnInfo]] = {}
        for table, column, data_type, nullable in rows:
            by_table.setdefault(table, []).append(
                ColumnInfo(column, str(data_type).lower(), nullable == "YES")
            )
        self._columns = {t: [c.name for c in cols] for t, cols in by_table.items()}
        schema: SourceSchema = {}
        for table, columns in by_table.items():
            count = cursor.execute(f"SELECT count(*) FROM {self._qualified(table)}").fetchone()
            rows_here = int(count[0]) if count else 0
            schema[table] = TableSchema(columns=columns, row_count=-1, row_estimate=rows_here)
        return schema

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        cursor = self._cursor()
        with self._lock:
            self.stats.full_fetches += 1
        result = cursor.execute(self._select(table, SliceSpec.FULL, max_rows))
        columns = tuple(d[0] for d in result.description)
        rows = result.fetchall()
        with self._lock:
            self.stats.rows_sent += len(rows)
        return RowSet(columns=columns, rows=rows)

    def fetch_slice(
        self, table: str, spec: SliceSpec = SliceSpec.FULL, max_rows: int | None = None
    ) -> RowStream:
        cursor = self._cursor()
        sql = self._select(table, spec, max_rows)
        with self._lock:
            self.stats.slice_fetches += 1
        self._note(f"slice {table}: {spec.describe()}")
        reader = cursor.execute(sql).to_arrow_reader(self._batch_rows)
        stream = ArrowRowStream(_CountingReader(reader, self.stats, self._lock))
        if self.export_max_rows is not None:
            stream.row_limit = self.export_max_rows  # type: ignore[attr-defined]
        return stream

    def estimate_slice(self, table: str, spec: SliceSpec = SliceSpec.FULL) -> int | None:
        if table not in self._columns:
            return None
        cursor = self._cursor()
        with self._lock:
            self.stats.estimates += 1
        sql = f"SELECT count(*) FROM {self._qualified(table)}"
        if spec.predicates:
            sql += " WHERE " + " AND ".join(predicate_sql(p) for p in spec.predicates)
        row = cursor.execute(sql).fetchone()
        return int(row[0]) if row else None

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            pushdown=self._pushdown, slices=True, arrow=True, row_estimates=True
        )

    @property
    def supports_pushdown(self) -> bool:
        return self._pushdown

    # -- PushdownCapable ----------------------------------------------------

    def execute_query(self, sql: str) -> RowSet:
        if not self._pushdown:
            raise QueryError(f"{self.name} does not accept raw SQL")
        cursor = self._cursor()
        cursor.execute(f"USE {quote(self._catalog)}")
        with self._lock:
            self.stats.pushdowns += 1
        self._note(f"pushdown: {sql[:160]}")
        try:
            result = cursor.execute(sql)
            columns = tuple(d[0] for d in result.description)
            rows = result.fetchall()
        except duckdb.Error as e:
            raise QueryError(f"Source {self.name} rejected the query: {e}") from e
        return RowSet(columns=columns, rows=rows)

    def fetch_filtered(
        self,
        table: str,
        filters: Mapping[str, object],
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> RowSet:
        spec = SliceSpec(
            columns=frozenset(columns) if columns else None,
            predicates=tuple(Predicate(c, "eq", v) for c, v in filters.items()),
            limit=limit,
        )
        cursor = self._cursor()
        result = cursor.execute(self._select(table, spec, None))
        names = tuple(d[0] for d in result.description)
        return RowSet(columns=names, rows=result.fetchall())
