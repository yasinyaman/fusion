"""DuckDB implementation of the AnalyticsStore port.

Owns the connection, the lock that serializes every DuckDB call, and the
``enable_external_access`` security latch. Rows travel in and out as domain
``RowSet`` values; Arrow is used only as the ingest vehicle.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from fusion.domain.errors import BackupError, QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.models import (
    MV_PREFIX,
    ColumnInfo,
    RowSet,
    RowStream,
    TableRef,
    TableSchema,
    TableSize,
)

logger = logging.getLogger(__name__)

_SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)?$", re.IGNORECASE)

_SQL_TYPES = {
    "integer": "BIGINT",
    "int": "BIGINT",
    "int2": "BIGINT",
    "int4": "BIGINT",
    "int8": "BIGINT",
    "smallint": "BIGINT",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "double precision": "DOUBLE",
    "float": "DOUBLE",
    "real": "DOUBLE",
    "decimal": "DECIMAL(38,9)",
    "numeric": "DECIMAL(38,9)",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ",
    "timestamp with time zone": "TIMESTAMPTZ",
    "timestamp without time zone": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    "blob": "BLOB",
    "bytes": "BLOB",
    "bytea": "BLOB",
    "json": "JSON",
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_qualified(name: str) -> str:
    """Quote ``schema.table``; only the first dot separates the two parts."""
    schema, separator, table = name.partition(".")
    if not separator:
        return _quote(name)
    return f"{_quote(schema)}.{_quote(table)}"


def _check_size(value: str, label: str) -> str:
    if not _SIZE_RE.match(value.strip()):
        raise ValueError(f"Invalid {label}: {value!r} (expected e.g. '4GB', '512MB')")
    return value.strip()


class DuckDBStore:
    """AnalyticsStore backed by an in-memory or file-based DuckDB database."""

    def __init__(
        self,
        database: str = ":memory:",
        threads: int = 4,
        memory_limit: str = "4GB",
        external_access: bool = False,
        max_temp_directory_size: str | None = None,
    ) -> None:
        self._database = database or ":memory:"
        self._threads = int(threads)
        self._memory_limit = _check_size(memory_limit, "memory_limit")
        self._external_access = external_access
        self._max_temp_directory_size = (
            _check_size(max_temp_directory_size, "max_temp_directory_size")
            if max_temp_directory_size
            else None
        )
        self._lock = threading.Lock()
        self._conn = self._open()
        logger.info(
            "DuckDBStore opened (db=%s, threads=%d, memory=%s, external_access=%s)",
            self._database,
            self._threads,
            self._memory_limit,
            self._external_access,
        )

    # -- connection lifecycle -----------------------------------------------

    def _open(self) -> duckdb.DuckDBPyConnection:
        """Open a connection and apply every setting, including the security latch.

        Called on construction and again by ``restore_from`` so a reopened
        connection can never silently lose the latch.
        """
        conn = duckdb.connect(self._database)
        conn.execute(f"SET threads TO {self._threads}")
        conn.execute(f"SET memory_limit = '{self._memory_limit}'")
        if self._max_temp_directory_size:
            conn.execute(f"SET max_temp_directory_size = '{self._max_temp_directory_size}'")
        if not self._external_access:
            # One-way latch: blocks read_csv/read_parquet/glob/ATTACH/COPY/httpfs.
            conn.execute("SET enable_external_access = FALSE")
        return conn

    @property
    def database_path(self) -> Path | None:
        return None if self._database == ":memory:" else Path(self._database)

    @property
    def external_access_enabled(self) -> bool:
        return self._external_access

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        logger.info("DuckDBStore closed")

    # -- schema management --------------------------------------------------

    def create_schema(self, name: str) -> None:
        _validate_plain_identifier(name, "schema name")
        with self._lock:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote(name)}")

    def drop_schema(self, name: str) -> None:
        _validate_plain_identifier(name, "schema name")
        with self._lock:
            self._conn.execute(f"DROP SCHEMA IF EXISTS {_quote(name)} CASCADE")

    def materialize(self, table: TableRef, rows: RowSet, schema: TableSchema | None = None) -> int:
        target = _quote_qualified(table.full_name)
        with self._lock:
            if rows.is_empty:
                self._create_empty_table(target, schema)
                return 0
            arrow_table = _rowset_to_arrow(rows)
            tmp = f"_fusion_ingest_{uuid.uuid4().hex}"
            self._conn.register(tmp, arrow_table)
            try:
                self._conn.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {tmp}")
            finally:
                self._conn.unregister(tmp)
        return len(rows)

    def _create_empty_table(self, target: str, schema: TableSchema | None) -> None:
        columns = schema.columns if schema else []
        if columns:
            cols = ", ".join(f"{_quote(c.name)} {_sql_type(c.type)}" for c in columns)
            self._conn.execute(f"CREATE OR REPLACE TABLE {target} ({cols})")
        else:
            self._conn.execute(
                f"CREATE OR REPLACE TABLE {target} AS SELECT NULL AS _empty WHERE FALSE"
            )

    # -- streaming ingest ---------------------------------------------------

    def materialize_stream(
        self, table_name: str, stream: RowStream, schema: TableSchema | None = None
    ) -> int:
        """Replace ``table_name`` with ``stream``, one batch at a time."""
        return self._ingest(table_name, stream, schema, replace=True)

    def append_stream(self, table_name: str, stream: RowStream) -> int:
        """Add ``stream``'s rows to ``table_name`` (created when it does not exist)."""
        return self._ingest(table_name, stream, None, replace=False)

    def _ingest(
        self, table_name: str, stream: RowStream, schema: TableSchema | None, replace: bool
    ) -> int:
        """Write a stream batch by batch; the lock is held per batch, never across.

        Holding it across the whole stream would block every query for as
        long as the HTTP response takes to arrive.
        """
        target = _quote_qualified(table_name)
        exists = self._table_exists(table_name)
        typed = schema is not None and bool(schema.columns)
        if replace and typed:
            # A declared schema wins over whatever the first batch looks like,
            # so later batches are cast into it instead of failing.
            with self._lock:
                self._create_empty_table(target, schema)
            ready = True
        else:
            ready = exists and not replace
            if replace and exists:
                self.drop_table(table_name)
        total = 0
        try:
            for batch in _stream_batches(stream):
                if batch.num_columns == 0:
                    continue
                self._write_batch(target, batch, append=ready)
                ready = True
                total += batch.num_rows
        finally:
            stream.close()
        if not ready:
            with self._lock:
                self._create_empty_table(target, schema)
        return total

    def _write_batch(self, target: str, batch: pa.Table, append: bool) -> None:
        tmp = f"_fusion_ingest_{uuid.uuid4().hex}"
        with self._lock:
            self._conn.register(tmp, batch)
            try:
                if not append:
                    self._conn.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {tmp}")
                    return
                # Insert by column name, not by position: batches may arrive
                # in a different order, carry an extra column, or hold a value
                # that only looked numeric until now.
                self._conn.execute(self._casting_insert(target, tmp))
            except duckdb.Error as e:
                raise QueryError(f"Failed to write a batch into {target}: {e}") from e
            finally:
                self._conn.unregister(tmp)

    def _casting_insert(self, target: str, tmp: str) -> str:
        columns = self._conn.execute(f"DESCRIBE {target}").fetchall()
        casts = ", ".join(
            f"CAST({_quote(name)} AS {type_}) AS {_quote(name)}" for name, type_, *_ in columns
        )
        names = ", ".join(_quote(name) for name, *_ in columns)
        return f"INSERT INTO {target} ({names}) SELECT {casts} FROM {tmp}"

    def _table_exists(self, table_name: str) -> bool:
        schema, separator, table = table_name.partition(".")
        if not separator:
            schema, table = "main", table_name
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                [schema, table],
            ).fetchone()
        return row is not None

    def upsert(self, table_name: str, stream: RowStream, key_columns: Sequence[str]) -> int:
        """Replace rows matching on ``key_columns``, insert the rest."""
        keys = list(key_columns)
        if not keys:
            raise QueryError("upsert needs at least one key column")
        if not self._table_exists(table_name):
            return self.materialize_stream(table_name, stream)
        staging = f"{table_name}__stage"
        count = self.materialize_stream(staging, stream)
        target = _quote_qualified(table_name)
        stage = _quote_qualified(staging)
        try:
            with self._lock:
                try:
                    match = " AND ".join(f"t.{_quote(k)} = s.{_quote(k)}" for k in keys)
                    self._conn.execute(
                        f"DELETE FROM {target} AS t "
                        f"WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE {match})"
                    )
                    columns = self._conn.execute(f"DESCRIBE {target}").fetchall()
                    names = ", ".join(_quote(name) for name, *_ in columns)
                    self._conn.execute(
                        f"INSERT INTO {target} ({names}) SELECT {names} FROM {stage}"
                    )
                except duckdb.Error as e:
                    raise QueryError(f"Upsert into {table_name} failed: {e}") from e
        finally:
            self.drop_table(staging)
        return count

    def delete_where_in(self, table_name: str, column: str, values: Iterable[Any]) -> int:
        """Delete every row whose ``column`` appears in ``values``."""
        wanted = list(values)
        target = _quote_qualified(table_name)
        if not wanted:
            return 0
        tmp = f"_fusion_keys_{uuid.uuid4().hex}"
        arrow_keys = pa.table({"value": _to_arrow_array(wanted, column)})
        with self._lock:
            self._conn.register(tmp, arrow_keys)
            try:
                deleted = self._conn.execute(
                    f"SELECT COUNT(*) FROM {target} "
                    f"WHERE {_quote(column)} IN (SELECT value FROM {tmp})"
                ).fetchone()
                self._conn.execute(
                    f"DELETE FROM {target} WHERE {_quote(column)} IN (SELECT value FROM {tmp})"
                )
            except duckdb.Error as e:
                raise QueryError(f"Delete from {table_name} failed: {e}") from e
            finally:
                self._conn.unregister(tmp)
        return int(deleted[0]) if deleted else 0

    def table_size(self, table_name: str) -> TableSize:
        """Row count plus DuckDB's own estimate of the bytes held."""
        rows = self.count(table_name)
        schema, separator, table = table_name.partition(".")
        if not separator:
            schema, table = "main", table_name
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT estimated_size FROM duckdb_tables() "
                    "WHERE schema_name = ? AND table_name = ?",
                    [schema, table],
                ).fetchone()
            except duckdb.Error:
                row = None
        size = int(row[0]) if row and row[0] is not None else None
        return TableSize(rows=rows, bytes=size)

    def rename_table(self, old_name: str, new_name: str) -> None:
        """Rename a table inside its schema (publishing a staged load)."""
        old_schema, _, _ = old_name.partition(".")
        new_schema, separator, new_table = new_name.partition(".")
        if not separator:
            new_schema, new_table = old_schema, new_name
        if new_schema != old_schema:
            raise QueryError(f"Cannot rename across schemas: '{old_name}' -> '{new_name}'")
        with self._lock:
            try:
                self._conn.execute(f"DROP TABLE IF EXISTS {_quote_qualified(new_name)}")
                self._conn.execute(
                    f"ALTER TABLE {_quote_qualified(old_name)} RENAME TO {_quote(new_table)}"
                )
            except duckdb.Error as e:
                raise QueryError(f"Failed to rename '{old_name}' to '{new_name}': {e}") from e

    def create_table_as(self, table_name: str, select_sql: str) -> None:
        if not table_name.startswith(MV_PREFIX) or not IDENTIFIER_RE.match(table_name):
            raise QueryError(
                f"create_table_as is reserved for materialized views (mv_*): '{table_name}'"
            )
        with self._lock:
            try:
                self._conn.execute(
                    f"CREATE OR REPLACE TABLE {_quote(table_name)} AS ({select_sql})"
                )
            except duckdb.Error as e:
                raise QueryError(f"Failed to create table '{table_name}': {e}") from e

    def drop_table(self, table_name: str) -> None:
        with self._lock:
            self._conn.execute(f"DROP TABLE IF EXISTS {_quote_qualified(table_name)}")

    # -- querying -----------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> RowSet:
        with self._lock:
            try:
                cursor = (
                    self._conn.execute(sql, list(params))
                    if params is not None
                    else self._conn.execute(sql)
                )
                if cursor.description is None:
                    return RowSet.empty()
                columns = tuple(desc[0] for desc in cursor.description)
                rows = cursor.fetchall()
            except duckdb.Error as e:
                raise QueryError(f"Query execution failed: {e}") from e
        return RowSet(columns=columns, rows=rows)

    def describe(self, table_name: str) -> list[ColumnInfo]:
        escaped = table_name.replace("'", "''")
        with self._lock:
            try:
                info = self._conn.execute(f"PRAGMA table_info('{escaped}')").fetchall()
            except duckdb.Error as e:
                raise QueryError(f"Cannot describe '{table_name}': {e}") from e
        # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        return [ColumnInfo(name=row[1], type=row[2], nullable=not row[3]) for row in info]

    def count(self, table_name: str) -> int:
        with self._lock:
            try:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_qualified(table_name)}"
                ).fetchone()
            except duckdb.Error as e:
                raise QueryError(f"Cannot count '{table_name}': {e}") from e
        return int(row[0]) if row else 0

    # -- backup / restore ---------------------------------------------------

    def export_to(self, directory: Path) -> None:
        if not self._external_access:
            raise BackupError(
                "EXPORT DATABASE needs DuckDB external access "
                "(set FUSION_DUCKDB_EXTERNAL_ACCESS=true)."
            )
        target = str(directory).replace("'", "''")
        with self._lock:
            try:
                self._conn.execute(f"EXPORT DATABASE '{target}' (FORMAT PARQUET)")
            except duckdb.Error as e:
                raise BackupError(f"EXPORT DATABASE failed: {e}") from e

    def import_from(self, directory: Path) -> None:
        if not self._external_access:
            raise BackupError(
                "IMPORT DATABASE needs DuckDB external access "
                "(set FUSION_DUCKDB_EXTERNAL_ACCESS=true)."
            )
        source = str(directory).replace("'", "''")
        with self._lock:
            try:
                self._conn.execute(f"IMPORT DATABASE '{source}'")
            except duckdb.Error as e:
                raise BackupError(f"IMPORT DATABASE failed: {e}") from e

    def snapshot_to(self, file: Path) -> None:
        db_path = self.database_path
        if db_path is None:
            raise BackupError("Cannot snapshot an in-memory database; use export_to instead.")
        with self._lock:
            self._conn.execute("CHECKPOINT")
            shutil.copy2(db_path, file)

    def restore_from(self, file: Path) -> None:
        db_path = self.database_path
        if db_path is None:
            raise BackupError("Cannot restore into an in-memory database; use import_from instead.")
        if not file.exists():
            raise BackupError(f"Backup file not found: {file}")
        with self._lock:
            self._conn.close()
            shutil.copy2(file, db_path)
            # Reopen through _open() so threads/memory_limit/external-access
            # settings are re-applied to the fresh connection.
            self._conn = self._open()
        logger.info("Restored %s from %s", db_path, file)


# -- helpers ----------------------------------------------------------------


def _validate_plain_identifier(name: str, label: str) -> None:
    if not IDENTIFIER_RE.match(name) or "." in name:
        raise QueryError(f"Invalid {label}: '{name}'")


def _sql_type(type_name: str) -> str:
    return _SQL_TYPES.get(type_name.lower(), "VARCHAR")


def _rowset_to_arrow(rows: RowSet) -> pa.Table:
    """Column-wise Arrow conversion with a per-column string fallback."""
    arrays = []
    for idx, name in enumerate(rows.columns):
        values = [row[idx] for row in rows.rows]
        arrays.append(_to_arrow_array(values, name))
    return pa.Table.from_arrays(arrays, names=list(rows.columns))


def _to_arrow_array(values: list[Any], name: str) -> pa.Array:
    if all(v is None for v in values):
        return pa.array(values, type=pa.string())
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
        logger.debug("Column %s has mixed types; storing as VARCHAR", name)
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())


def _stream_batches(stream: RowStream) -> Iterator[pa.Table]:
    """Arrow tables from a RowStream, using its Arrow reader when it has one."""
    reader = stream.arrow_reader()
    if reader is not None:
        for batch in reader:
            yield pa.Table.from_batches([batch]) if isinstance(batch, pa.RecordBatch) else batch
        return
    for rows in stream:
        yield _rowset_to_arrow(rows)
