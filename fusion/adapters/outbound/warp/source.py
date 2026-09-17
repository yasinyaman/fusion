"""WarpSource: DataSource + PushdownCapable over the Warp REST API."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fusion.adapters.outbound.warp.capabilities import WarpCapabilities, from_info
from fusion.adapters.outbound.warp.http import (
    DEFAULT_API_KEY_HEADER,
    DEFAULT_TIMEOUT,
    HttpTransport,
    HttpTransportError,
    WarpHttpClient,
    build_transport,
)
from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.models import ColumnInfo, RowSet, SourceSchema, TableSchema

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 1000
SCHEMA_SAMPLE_ROWS = 5


class WarpSource:
    """Config keys (via ``from_config``):

    type: "warp" · base_url · database (defaults to the source name) · api_key ·
    api_key_header · timeout · page_size · max_retries · backoff_factor ·
    pool_size · pool_max_overflow · circuit_breaker_threshold ·
    circuit_breaker_timeout

    ``connect`` reads ``/info`` to learn what the Warp can do (see
    ``WarpCapabilities``) and adapts: URL layout, pushdown availability.
    """

    source_type = "warp"

    def __init__(
        self,
        name: str,
        client: WarpHttpClient,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.name = name
        self._client = client
        self._page_size = page_size
        self._tables: list[str] = []
        self._connected = False
        self._caps = WarpCapabilities()
        self._raw_query_denied = False
        self._layout_fallback_tried = False

    @classmethod
    def from_config(cls, name: str, config: Mapping[str, Any]) -> WarpSource:
        base_url = str(config.get("base_url", "http://localhost:8080"))
        database = str(config.get("database", name))
        timeout = float(config.get("timeout", DEFAULT_TIMEOUT))
        transport: HttpTransport | None = config.get("transport")
        if transport is None:
            transport = build_transport(
                api_key=config.get("api_key"),
                api_key_header=str(config.get("api_key_header") or DEFAULT_API_KEY_HEADER),
                timeout=timeout,
                max_retries=int(config.get("max_retries", 3)),
                backoff_factor=float(config.get("backoff_factor", 2.0)),
                pool_size=int(config.get("pool_size", 10)),
                pool_max_overflow=int(config.get("pool_max_overflow", 5)),
                circuit_breaker_threshold=int(config.get("circuit_breaker_threshold", 5)),
                circuit_breaker_timeout=float(config.get("circuit_breaker_timeout", 60.0)),
                breaker_name=f"warp:{name}",
            )
        client = WarpHttpClient(base_url, database, transport, timeout=timeout)
        return cls(name, client, page_size=int(config.get("page_size", DEFAULT_PAGE_SIZE)))

    # -- DataSource ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tables(self) -> list[str]:
        return list(self._tables)

    @property
    def database(self) -> str:
        return self._client.database

    @property
    def base_url(self) -> str:
        return self._client.base_url

    @property
    def warp_capabilities(self) -> WarpCapabilities:
        """What the connected Warp reported (defaults before ``connect``)."""
        return self._caps

    def connect(self) -> None:
        try:
            self._client.health()
        except ConnectionError as e:
            raise ConnectionError(f"Cannot connect to Warp at {self._client.base_url}: {e}") from e
        try:
            info = self._client.info()
        except ConnectionError as e:
            raise ConnectionError(f"Failed to discover tables from Warp: {e}") from e
        self._tables = extract_tables(info, self._client.database)
        self._caps = from_info(info)
        self._client.configure(api_prefix=self._caps.api_prefix, db_prefixed=True)
        self._connected = True
        logger.info(
            "Connected to Warp %s at %s (db=%s, tables=%d, schema=%s, export=%s, raw_query=%s)",
            self._caps.version or "(unknown version)",
            self._client.base_url,
            self._client.database,
            len(self._tables),
            self._caps.schema,
            ",".join(self._caps.export_formats) or "no",
            self._caps.raw_query,
        )

    def _table_page(self, table: str, limit: int, offset: int = 0) -> Any:
        """One page of ``table``, retrying once with the pre-0.10 URL layout.

        Warp < 0.10 mounted a *single* database without the ``/{db}`` segment,
        so the db-scoped URL 404s there. The fallback is tried once per source
        and only for a Warp that did not advertise its layout.
        """
        try:
            return self._client.table_page(table, limit=limit, offset=offset)
        except HttpTransportError as e:
            if e.status != 404 or self._layout_fallback_tried or not self._caps.legacy:
                raise
            if not self._client.db_prefixed:
                raise
        self._layout_fallback_tried = True
        self._client.configure(db_prefixed=False)
        try:
            page = self._client.table_page(table, limit=limit, offset=offset)
        except ConnectionError:
            # Not a layout problem (the table is simply unknown): put it back.
            self._client.configure(db_prefixed=True)
            raise
        logger.info(
            "Warp at %s serves un-prefixed table routes (single database, pre-0.10); using %s",
            self._client.base_url,
            self._client.api_root,
        )
        return page

    def close(self) -> None:
        self._client.close()
        self._connected = False
        logger.info("WarpSource closed for %s", self.name)

    def discover_schema(self) -> SourceSchema:
        self._require_connected()
        schema: SourceSchema = {}
        for table in self._tables:
            try:
                data = self._table_page(table, limit=SCHEMA_SAMPLE_ROWS)
                rows = RowSet.from_records(extract_rows(data))
                schema[table] = TableSchema(
                    columns=infer_columns(rows),
                    row_count=extract_row_count(data) if not rows.is_empty else 0,
                )
            except Exception as e:
                logger.warning("Failed to get schema for table %s: %s", table, e)
                schema[table] = TableSchema(columns=[], row_count=0)
        return schema

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        self._require_connected()
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_size = self._page_size
            if max_rows is not None:
                page_size = min(page_size, max_rows - len(records))
            try:
                data = self._table_page(table, limit=page_size, offset=offset)
            except ConnectionError as e:
                raise QueryError(f"Failed to fetch data from {table}: {e}") from e
            rows = extract_rows(data)
            if not rows:
                break
            records.extend(rows)
            if max_rows is not None and len(records) >= max_rows:
                del records[max_rows:]
                logger.warning("Table %s truncated to max_rows=%d during ingest", table, max_rows)
                break
            if len(rows) < self._page_size:
                break
            offset += self._page_size
        logger.info("Fetched %d rows from %s", len(records), table)
        return RowSet.from_records(records)

    @property
    def supports_pushdown(self) -> bool:
        """Raw SQL is available unless Warp said otherwise, or refused one (403)."""
        return self._caps.raw_query and not self._raw_query_denied

    # -- PushdownCapable ----------------------------------------------------

    def execute_query(self, sql: str) -> RowSet:
        self._require_connected()
        try:
            data = self._client.query(sql)
        except HttpTransportError as e:
            if e.status == 403:
                # Raw query is disabled on this Warp (default, and forced in
                # production); stop trying so the breaker and the logs stay quiet.
                self._raw_query_denied = True
                logger.warning(
                    "Warp at %s refuses raw queries (HTTP 403); pushdown disabled for '%s'",
                    self._client.base_url,
                    self.name,
                )
            raise QueryError(f"Warp query execution failed: {e}") from e
        except ConnectionError as e:
            raise QueryError(f"Warp query execution failed: {e}") from e
        return RowSet.from_records(extract_rows(data))

    def fetch_filtered(
        self,
        table: str,
        filters: Mapping[str, object],
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> RowSet:
        self._require_connected()
        sql = build_filter_sql(table, filters, columns, limit)
        try:
            return self.execute_query(sql)
        except QueryError:
            logger.warning("execute_query failed for %s, falling back to full fetch", table)
        rows = self.fetch_table(table)
        return filter_rowset(rows, filters, columns, limit)

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")


# -- pure helpers (also used by discovery) -----------------------------------


def extract_tables(info: Any, database: str) -> list[str]:
    """Table names from a Warp ``/info`` payload (several shapes supported)."""
    tables: list[str] = []

    def add_all(raw: Any) -> None:
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, str):
                tables.append(item)
            elif isinstance(item, dict) and "name" in item:
                tables.append(item["name"])

    if isinstance(info, dict):
        if "tables" in info:
            add_all(info["tables"])
        elif "databases" in info:
            dbs = info["databases"]
            if isinstance(dbs, dict):  # Format A (Warp): {"db": {"tables": [...]}}
                for db_name, db_info in dbs.items():
                    if db_name == database or not database:
                        add_all(db_info.get("tables", []) if isinstance(db_info, dict) else [])
            elif isinstance(dbs, list):  # Format B: [{"name": "db", "tables": [...]}]
                for db in dbs:
                    if isinstance(db, dict) and (db.get("name", "") == database or not database):
                        add_all(db.get("tables", []))
    elif isinstance(info, list):
        add_all(info)
    return tables


def extract_rows(data: Any) -> list[dict[str, Any]]:
    """Row records from a Warp table/query payload."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "rows", "results", "items", "records"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def extract_row_count(data: Any) -> int:
    """Total row count from response metadata, or -1 when unknown."""
    if isinstance(data, dict):
        for key in ("total", "count", "total_count", "total_rows"):
            if key in data and isinstance(data[key], int):
                return data[key]
    return -1


def infer_column_type(values: Sequence[Any]) -> str:
    """SQL-ish type name for a sample of Python values (JSON-decoded)."""
    kinds: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer")
        elif isinstance(value, float):
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


def infer_columns(rows: RowSet) -> list[ColumnInfo]:
    columns = []
    for idx, name in enumerate(rows.columns):
        values = [row[idx] for row in rows.rows]
        columns.append(
            ColumnInfo(
                name=name,
                type=infer_column_type(values),
                nullable=any(v is None for v in values),
            )
        )
    return columns


def build_filter_sql(
    table: str,
    filters: Mapping[str, object],
    columns: Sequence[str] | None,
    limit: int | None,
) -> str:
    col_clause = ", ".join(columns) if columns else "*"
    where_parts = []
    for col, val in filters.items():
        if isinstance(val, str):
            where_parts.append(f"{col} = '{val.replace(chr(39), chr(39) * 2)}'")
        else:
            where_parts.append(f"{col} = {val}")
    sql = f"SELECT {col_clause} FROM {table}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if limit:
        sql += f" LIMIT {limit}"
    return sql


def filter_rowset(
    rows: RowSet,
    filters: Mapping[str, object],
    columns: Sequence[str] | None,
    limit: int | None,
) -> RowSet:
    """In-memory equality filter / projection / limit (pushdown fallback)."""
    records = rows.to_records()
    for col, val in filters.items():
        if col in rows.columns:
            records = [r for r in records if r.get(col) == val]
    if columns:
        keep = [c for c in columns if c in rows.columns]
        if keep:
            records = [{c: r[c] for c in keep} for r in records]
    if limit:
        records = records[:limit]
    return RowSet.from_records(records)
