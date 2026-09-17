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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from fusion.domain.errors import BackupError, QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.models import MV_PREFIX, ColumnInfo, RowSet, TableRef, TableSchema

logger = logging.getLogger(__name__)

_SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)?$", re.IGNORECASE)

_SQL_TYPES = {
    "integer": "BIGINT",
    "int": "BIGINT",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "float": "DOUBLE",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "date": "DATE",
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_qualified(name: str) -> str:
    return ".".join(_quote(part) for part in name.split("."))


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
