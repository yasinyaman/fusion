"""Outbound port: the analytical store that executes SQL (DuckDB in prod).

The adapter owns the connection, its lock and the security latch
(``enable_external_access``); nothing above this port ever sees a raw
connection.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from fusion.domain.models import (
    ColumnInfo,
    RowSet,
    RowStream,
    TableRef,
    TableSchema,
    TableSize,
)


class AnalyticsStore(Protocol):
    @property
    def database_path(self) -> Path | None:
        """Backing file, or ``None`` for an in-memory database."""
        ...

    @property
    def external_access_enabled(self) -> bool:
        """Whether the store may touch the filesystem/network (backups need it)."""
        ...

    # -- schema management --------------------------------------------------

    def create_schema(self, name: str) -> None: ...

    def drop_schema(self, name: str) -> None: ...

    def materialize(self, table: TableRef, rows: RowSet, schema: TableSchema | None = None) -> int:
        """CREATE OR REPLACE ``table`` from ``rows``; returns the row count.

        When ``rows`` is empty and ``schema`` is given, an empty table with
        the schema's columns is created so queries against it still work.
        """
        ...

    def materialize_stream(
        self, table_name: str, stream: RowStream, schema: TableSchema | None = None
    ) -> int:
        """Replace ``table_name`` with the contents of ``stream``; returns the row count.

        Batches are written as they arrive, so a table far larger than the
        process's memory can be ingested. Implementations must not hold their
        lock while waiting for the next batch: the producer is usually an
        HTTP response.
        """
        ...

    def append_stream(self, table_name: str, stream: RowStream) -> int:
        """Add ``stream``'s rows to ``table_name`` (creating it when absent)."""
        ...

    def upsert(self, table_name: str, stream: RowStream, key_columns: Sequence[str]) -> int:
        """Replace rows whose ``key_columns`` match, insert the rest.

        Returns how many rows arrived from ``stream``.
        """
        ...

    def delete_where_in(self, table_name: str, column: str, values: Iterable[Any]) -> int:
        """Delete rows whose ``column`` is one of ``values``; returns how many went."""
        ...

    def table_size(self, table_name: str) -> TableSize:
        """Rows and (when the store knows) bytes held by a table."""
        ...

    def rename_table(self, old_name: str, new_name: str) -> None:
        """Rename within the same schema (used to publish a staged load atomically)."""
        ...

    def create_table_as(self, table_name: str, select_sql: str) -> None:
        """``CREATE OR REPLACE TABLE table_name AS (select_sql)``.

        The only non-SELECT path in the system; callers validate ``select_sql``
        through the SqlValidator first and only use it for ``mv_*`` tables.
        """
        ...

    def drop_table(self, table_name: str) -> None: ...

    # -- querying -----------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> RowSet:
        """Run ``sql`` (with optional ``?`` params); raises QueryError."""
        ...

    def describe(self, table_name: str) -> list[ColumnInfo]: ...

    def count(self, table_name: str) -> int: ...

    # -- backup / restore ---------------------------------------------------

    def export_to(self, directory: Path) -> None:
        """EXPORT DATABASE into ``directory``; raises BackupError if not allowed."""
        ...

    def import_from(self, directory: Path) -> None: ...

    def snapshot_to(self, file: Path) -> None:
        """Copy the backing file to ``file``; raises BackupError when in-memory."""
        ...

    def restore_from(self, file: Path) -> None:
        """Replace the backing file with ``file`` and reopen with the same settings."""
        ...

    def close(self) -> None: ...
