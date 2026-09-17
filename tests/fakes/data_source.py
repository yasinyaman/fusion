"""FakeDataSource: an in-memory DataSource (+ optional PushdownCapable)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.models import (
    ColumnInfo,
    ListRowStream,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
    TableSchema,
)
from fusion.domain.slices import SliceSpec
from tests.fakes.warp_transport import run_mock_sql


def _infer_type(values: list[Any]) -> str:
    kinds = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            kinds.add("boolean")
        elif isinstance(v, int):
            kinds.add("integer")
        elif isinstance(v, float):
            kinds.add("double")
        else:
            kinds.add("varchar")
    if not kinds:
        return "varchar"
    if kinds == {"integer"}:
        return "integer"
    if kinds <= {"integer", "double"}:
        return "double"
    if kinds == {"boolean"}:
        return "boolean"
    return "varchar"


class FakeDataSource:
    """Serves tables from memory and records every call for assertions."""

    source_type = "fake"

    def __init__(
        self,
        name: str,
        tables: Mapping[str, list[dict[str, Any]] | RowSet] | None = None,
        *,
        pushdown: bool = False,
        sql_executor: Callable[[str], RowSet] | None = None,
        fail_connect: bool = False,
        slices: bool = True,
        arrow: bool = False,
        row_estimates: dict[str, int] | None = None,
        batch_size: int = 10_000,
    ) -> None:
        self.name = name
        self._tables: dict[str, RowSet] = {
            t: v if isinstance(v, RowSet) else RowSet.from_records(v)
            for t, v in (tables or {}).items()
        }
        self.pushdown = pushdown
        self.sql_executor = sql_executor
        self.fail_connect = fail_connect
        self.slices = slices
        self.arrow = arrow
        #: Pretend a table is huge without holding the rows (planner tests).
        self.row_estimates = dict(row_estimates or {})
        self.batch_size = batch_size
        self.connected = False
        self.closed = False
        self.calls: list[tuple[Any, ...]] = []

    # -- DataSource ---------------------------------------------------------

    def connect(self) -> None:
        self.calls.append(("connect",))
        if self.fail_connect:
            raise ConnectionError(f"fake source {self.name} refused to connect")
        self.connected = True

    def close(self) -> None:
        self.calls.append(("close",))
        self.connected = False
        self.closed = True

    def discover_schema(self) -> SourceSchema:
        self.calls.append(("discover_schema",))
        schema: SourceSchema = {}
        for name, rows in self._tables.items():
            columns = [
                ColumnInfo(
                    col,
                    _infer_type(rows.column(col)),
                    nullable=any(v is None for v in rows.column(col)),
                )
                for col in rows.columns
            ]
            schema[name] = TableSchema(
                columns=columns,
                row_count=len(rows),
                row_estimate=self.row_estimates.get(name, len(rows)),
            )
        return schema

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        self.calls.append(("fetch_table", table, max_rows))
        if not self.connected:
            raise ConnectionError("Not connected. Call connect() first.")
        if table not in self._tables:
            raise QueryError(f"Failed to fetch data from {table}: unknown table")
        rows = self._tables[table]
        return rows.head(max_rows) if max_rows is not None else rows

    def fetch_slice(
        self, table: str, spec: SliceSpec = SliceSpec.FULL, max_rows: int | None = None
    ) -> RowStream:
        self.calls.append(("fetch_slice", table, spec, max_rows))
        if not self.connected:
            raise ConnectionError("Not connected. Call connect() first.")
        if table not in self._tables:
            raise QueryError(f"Failed to fetch data from {table}: unknown table")
        records = [r for r in self._tables[table].to_records() if spec.matches(r)]
        if spec.columns is not None:
            keep = [c for c in self._tables[table].columns if c in spec.columns]
            records = [{c: r[c] for c in keep} for r in records]
        caps = [limit for limit in (spec.limit, max_rows) if limit is not None]
        if caps:
            records = records[: min(caps)]
        return ListRowStream.from_records(records, batch_size=self.batch_size)

    def estimate_slice(self, table: str, spec: SliceSpec = SliceSpec.FULL) -> int | None:
        self.calls.append(("estimate_slice", table, spec))
        if table not in self._tables:
            return None
        rows = self._tables[table]
        if spec.is_full or not spec.predicates:
            return self.row_estimates.get(table, len(rows))
        matching = sum(1 for r in rows.to_records() if spec.matches(r))
        if table not in self.row_estimates or not len(rows):
            return matching
        # Scale the real count up to the pretended table size, so a fake
        # "huge" table also reports a proportionally large slice.
        return round(matching * self.row_estimates[table] / len(rows))

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            pushdown=self.pushdown,
            slices=self.slices,
            arrow=self.arrow,
            row_estimates=True,
        )

    @property
    def supports_pushdown(self) -> bool:
        return self.pushdown

    # -- PushdownCapable ----------------------------------------------------

    def execute_query(self, sql: str) -> RowSet:
        self.calls.append(("execute_query", sql))
        if self.sql_executor is None:
            raise QueryError("fake source has no SQL executor")
        return self.sql_executor(sql)

    def fetch_filtered(
        self,
        table: str,
        filters: Mapping[str, object],
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> RowSet:
        self.calls.append(("fetch_filtered", table, dict(filters), columns, limit))
        rows = self._tables[table]
        records = rows.to_records()
        for col, val in filters.items():
            records = [r for r in records if str(r.get(col)) == str(val)]
        if columns:
            records = [{c: r[c] for c in columns if c in r} for r in records]
        if limit:
            records = records[:limit]
        return RowSet.from_records(records)

    # -- test helpers -------------------------------------------------------

    def calls_named(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]

    def set_table(self, table: str, records: list[dict[str, Any]]) -> None:
        self._tables[table] = RowSet.from_records(records)


class FakeSourceFactory:
    """SourceFactory building FakeDataSources from config; remembers them.

    Config keys: ``tables`` (name -> records), ``pushdown`` (bool),
    ``sql_executor`` (callable, defaults to the mock SQL interpreter when
    pushdown is on), ``fail_connect`` (bool), ``slices``/``arrow`` (capability
    flags), ``row_estimates`` (name -> pretended source-side size) and
    ``batch_size``.
    """

    def __init__(self) -> None:
        self.sources: dict[str, FakeDataSource] = {}

    def __call__(self, name: str, config: Mapping[str, Any]) -> FakeDataSource:
        tables = dict(config.get("tables", {}))
        executor = config.get("sql_executor")
        if executor is None and config.get("pushdown"):

            def executor(sql: str) -> RowSet:
                return RowSet.from_records(run_mock_sql(sql, tables))

        source = FakeDataSource(
            name,
            tables,
            pushdown=bool(config.get("pushdown", False)),
            sql_executor=executor,
            fail_connect=bool(config.get("fail_connect", False)),
            slices=bool(config.get("slices", True)),
            arrow=bool(config.get("arrow", False)),
            row_estimates=config.get("row_estimates"),
            batch_size=int(config.get("batch_size", 10_000)),
        )
        self.sources[name] = source
        return source
