"""Outbound port: an external data source (e.g. Warp over REST)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from fusion.domain.models import RowSet, SourceSchema


@runtime_checkable
class DataSource(Protocol):
    """A source of tables that can be discovered and fetched into the store."""

    name: str
    source_type: str

    def connect(self) -> None:
        """Establish the connection and discover available tables."""
        ...

    def close(self) -> None: ...

    def discover_schema(self) -> SourceSchema:
        """Column metadata (and row count when known) for every table."""
        ...

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        """All rows of ``table`` (stops after ``max_rows`` when given)."""
        ...

    @property
    def supports_pushdown(self) -> bool:
        """True when the source also implements ``PushdownCapable``."""
        ...


@runtime_checkable
class PushdownCapable(Protocol):
    """A source that can run SQL on its own backend (query pushdown)."""

    def execute_query(self, sql: str) -> RowSet:
        """Run ``sql`` on the source database and return the rows."""
        ...

    def fetch_filtered(
        self,
        table: str,
        filters: Mapping[str, object],
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> RowSet:
        """Rows of ``table`` matching equality ``filters`` (server-side)."""
        ...


SourceFactory = Callable[[str, Mapping[str, Any]], DataSource]
"""``(name, config) -> DataSource``; the config carries a ``type`` key."""


class DatabaseDiscovery(Protocol):
    """Lists the databases a gateway exposes, before any source is created."""

    def discover_databases(
        self, base_url: str, api_key: str | None = None, timeout: float = 30.0
    ) -> list[str]: ...
