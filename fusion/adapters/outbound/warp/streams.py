"""RowStream implementations over a Warp response.

Three ways to read a slice, best first:

* ``ArrowRowStream`` — Warp's Arrow IPC export. Types survive the wire
  (decimals stay decimals, timestamps stay timestamps) and the batches go
  straight into DuckDB without ever becoming Python objects.
* ``NdjsonRowStream`` — one JSON object per line, still streamed.
* ``PagedRowStream`` — the plain list endpoint, page by page. This is the
  only option against Warp 0.9, which has no export endpoint.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from fusion.domain.errors import QueryError
from fusion.domain.models import ColumnInfo, RowSet, TableSchema
from fusion.domain.slices import Predicate, SliceSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from fusion.adapters.outbound.warp.http import ByteStream

    Pager = Callable[..., Any]
    """``(table, limit, offset, fields, filters) -> payload``."""

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10_000
#: Warp's list endpoint takes ``IN`` values in the URL, so long key lists are
#: requested in chunks and the pages concatenated.
PAGED_IN_CHUNK = 200

#: Arrow type prefix -> the type names Fusion's store understands.
_ARROW_KINDS: tuple[tuple[str, str], ...] = (
    ("bool", "boolean"),
    ("int", "integer"),
    ("uint", "integer"),
    ("float", "double"),
    ("double", "double"),
    ("halffloat", "double"),
    ("decimal", "decimal"),
    ("timestamp", "timestamp"),
    ("date", "date"),
    ("time", "time"),
    ("binary", "blob"),
    ("fixed_size_binary", "blob"),
)


def _arrow_type_name(arrow_type: Any) -> str:
    text = str(arrow_type).lower()
    for prefix, name in _ARROW_KINDS:
        if text.startswith(prefix):
            return name
    return "varchar"


class ArrowRowStream:
    """Rows from an Arrow IPC stream; the reader is handed to the store as-is."""

    def __init__(self, reader: Any, response: ByteStream | None = None) -> None:
        self._reader = reader
        self._response = response
        self._taken = False
        self._schema = reader.schema

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self._schema.names)

    @property
    def schema(self) -> TableSchema | None:
        """Column types as announced by Arrow (this is why Arrow is preferred)."""
        return TableSchema(
            columns=[
                ColumnInfo(field.name, _arrow_type_name(field.type), nullable=field.nullable)
                for field in self._schema
            ]
        )

    def arrow_reader(self) -> Any:
        """The Arrow reader itself, once; None afterwards."""
        if self._taken:
            return None
        self._taken = True
        return self._reader

    def __iter__(self) -> Iterator[RowSet]:
        if self._taken:
            return
        self._taken = True
        columns = self.columns
        for batch in self._reader:
            yield _rowset(batch.to_pylist(), columns)

    def close(self) -> None:
        if self._response is not None:
            self._response.close()


class NdjsonRowStream:
    """Rows from a newline-delimited JSON body."""

    def __init__(
        self,
        response: ByteStream,
        batch_size: int = DEFAULT_BATCH_SIZE,
        schema: TableSchema | None = None,
    ) -> None:
        self._response = response
        self._batch_size = batch_size
        self._schema = schema
        self._columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def schema(self) -> TableSchema | None:
        return self._schema

    def arrow_reader(self) -> Any:
        return None

    def __iter__(self) -> Iterator[RowSet]:
        batch: list[dict[str, Any]] = []
        for line in self._response.iter_lines():
            if not line:
                continue
            try:
                batch.append(json.loads(line))
            except ValueError as e:
                raise QueryError(f"Malformed NDJSON row from Warp: {e}") from e
            if len(batch) >= self._batch_size:
                yield self._batch(batch)
                batch = []
        if batch:
            yield self._batch(batch)

    def _batch(self, records: list[dict[str, Any]]) -> RowSet:
        if not self._columns:
            self._columns = _columns_of(records, self._schema)
        return _rowset(records, self._columns)

    def close(self) -> None:
        self._response.close()


class PagedRowStream:
    """Rows from the plain list endpoint, one page at a time.

    Used against a Warp without the export endpoint, and whenever an export
    is refused. Filters and the projection still go to the server; only the
    transport is less efficient.
    """

    def __init__(
        self,
        pager: Pager,
        table: str,
        spec: SliceSpec = SliceSpec.FULL,
        page_size: int = 1000,
        max_rows: int | None = None,
    ) -> None:
        self._pager = pager
        self._table = table
        self._spec = spec
        self._page_size = max(1, page_size)
        self._max_rows = _tightest(spec.limit, max_rows)
        self._columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def schema(self) -> TableSchema | None:
        return None

    def arrow_reader(self) -> Any:
        return None

    def __iter__(self) -> Iterator[RowSet]:
        from fusion.adapters.outbound.warp.source import extract_rows

        fields = sorted(self._spec.columns) if self._spec.columns is not None else None
        fetched = 0
        empty = True
        for predicates in _predicate_chunks(self._spec.predicates):
            offset = 0
            while True:
                remaining = None if self._max_rows is None else self._max_rows - fetched
                if remaining is not None and remaining <= 0:
                    return
                limit = self._page_size if remaining is None else min(self._page_size, remaining)
                page = extract_rows(
                    self._pager(
                        self._table,
                        limit=limit,
                        offset=offset,
                        fields=fields,
                        filters=predicates,
                    )
                )
                if not page:
                    break
                if remaining is not None and len(page) > remaining:
                    # The server ignored our limit; stop at what we asked for.
                    page = page[:remaining]
                fetched += len(page)
                if not self._columns:
                    self._columns = _columns_of(page, None)
                empty = False
                yield _rowset(page, self._columns)
                if len(page) < limit:
                    break
                offset += limit
        if empty:
            yield RowSet(columns=self._columns, rows=[])

    def close(self) -> None:
        return None


def _tightest(*limits: int | None) -> int | None:
    values = [limit for limit in limits if limit is not None]
    return min(values) if values else None


def _predicate_chunks(predicates: Sequence[Predicate]) -> list[tuple[Predicate, ...]]:
    """Split one long ``IN`` list into several requests; other predicates ride along.

    Only the first oversized ``IN`` is split: a second one would multiply the
    number of requests, and in practice a slice has at most one key list.
    """
    for index, predicate in enumerate(predicates):
        if predicate.op == "in" and len(predicate.value) > PAGED_IN_CHUNK:
            values = list(predicate.value)
            chunks = []
            for start in range(0, len(values), PAGED_IN_CHUNK):
                part = Predicate(predicate.column, "in", values[start : start + PAGED_IN_CHUNK])
                chunks.append((*predicates[:index], part, *predicates[index + 1 :]))
            return chunks
    return [tuple(predicates)]


def _columns_of(records: Sequence[dict[str, Any]], schema: TableSchema | None) -> tuple[str, ...]:
    """Column order for a batch: the declared schema first, else first-seen keys."""
    if schema is not None and schema.columns:
        return tuple(c.name for c in schema.columns)
    seen: dict[str, None] = {}
    for record in records:
        for key in record:
            seen.setdefault(key, None)
    return tuple(seen)


def _rowset(records: Sequence[dict[str, Any]], columns: tuple[str, ...]) -> RowSet:
    """Build a RowSet with a fixed column order, so every batch lines up."""
    if not columns:
        return RowSet.from_records(records)
    return RowSet(
        columns=columns,
        rows=[tuple(record.get(name) for name in columns) for record in records],
    )
