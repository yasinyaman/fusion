"""Outbound port: an external data source (e.g. Warp over REST)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from fusion.domain.models import (
    ListRowStream,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
)
from fusion.domain.slices import SliceSpec


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

    def fetch_slice(self, table: str, spec: SliceSpec, max_rows: int | None = None) -> RowStream:
        """Stream the rows and columns ``spec`` describes.

        A source that cannot filter or project server-side may fall back to
        ``fetch_slice_in_memory``; the result must be the same, only slower.
        """
        ...

    def estimate_slice(self, table: str, spec: SliceSpec) -> int | None:
        """How many rows ``spec`` would return, or None when the source cannot tell.

        This must be cheap: it is asked *before* deciding whether to fetch.
        """
        ...

    @property
    def capabilities(self) -> SourceCapabilities:
        """What this source can do (pushdown, slices, Arrow, size estimates)."""
        ...

    @property
    def supports_pushdown(self) -> bool:
        """True when the source also implements ``PushdownCapable``.

        Must agree with ``capabilities.pushdown``.
        """
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


def fetch_slice_in_memory(
    source: DataSource, table: str, spec: SliceSpec, max_rows: int | None = None
) -> RowStream:
    """``fetch_slice`` for sources that can only hand over whole tables.

    Fetches everything, then applies the predicates, the projection and the
    limit locally. Correct but not cheap: it is the fallback for sources
    without server-side filtering, never the fast path.
    """
    rows = source.fetch_table(table)
    records = [r for r in rows.to_records() if spec.matches(r)]
    if spec.columns is not None:
        keep = [c for c in rows.columns if c in spec.columns]
        records = [{c: r[c] for c in keep} for r in records]
    caps = [limit for limit in (spec.limit, max_rows) if limit is not None]
    if caps:
        records = records[: min(caps)]
    return ListRowStream.from_records(records)


SourceFactory = Callable[[str, Mapping[str, Any]], DataSource]
"""``(name, config) -> DataSource``; the config carries a ``type`` key."""


class DatabaseDiscovery(Protocol):
    """Lists the databases a gateway exposes, before any source is created."""

    def discover_databases(
        self, base_url: str, api_key: str | None = None, timeout: float = 30.0
    ) -> list[str]: ...
