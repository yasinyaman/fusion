"""ToolService: the 10 LLM tools, dispatched by name.

Every method returns a JSON-serializable dict; failures become
``{"error": "..."}`` so MCP/REST/SDK callers get one uniform shape.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from fusion.application.query import QueryService
from fusion.application.sources import SourceService
from fusion.application.tool_schemas import TOOL_NAMES
from fusion.application.views import MaterializedViewService
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import BackupError, GuardrailViolation, QueryError, SchemaError
from fusion.domain.identifiers import (
    ALLOWED_AGG_FUNCS,
    MAX_RESULT_ROWS,
    is_valid_view_name,
    validate_identifier,
)
from fusion.domain.models import QueryResult, RowSet, TableRef
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.data_source import PushdownCapable

logger = logging.getLogger(__name__)


class ToolService:
    def __init__(
        self,
        query: QueryService,
        sources: SourceService,
        views: MaterializedViewService,
        store: AnalyticsStore,
        catalog: SchemaCatalog,
        cache: QueryCache,
    ) -> None:
        self._query = query
        self._sources = sources
        self._views = views
        self._store = store
        self._catalog = catalog
        self._cache = cache
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            name: getattr(self, name) for name in TOOL_NAMES
        }

    # -- dispatch -----------------------------------------------------------

    def execute(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return handler(**dict(arguments or {}))
        except (GuardrailViolation, QueryError, SchemaError, BackupError) as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"Invalid arguments for {tool_name}: {e}"}
        except Exception as e:
            logger.exception("Tool execution error: %s", tool_name)
            return {"error": f"Internal error: {e}"}

    @property
    def names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    # -- tools --------------------------------------------------------------

    def list_sources(self) -> dict[str, Any]:
        sources = []
        for source_name, entry in self._catalog.get_all_sources().items():
            tables = []
            for table_name, schema in entry.tables.items():
                ref = TableRef(source_name, table_name)
                loaded = self._catalog.is_loaded(ref)
                row_count = schema.row_count
                if loaded:
                    try:
                        row_count = self._store.count(ref.full_name)
                    except QueryError:
                        pass
                tables.append(
                    {
                        "name": ref.full_name,
                        "row_count": row_count,
                        "columns": len(schema.columns),
                        "loaded": loaded,
                    }
                )
            sources.append({"source": source_name, "type": entry.type, "tables": tables})
        return {"sources": sources}

    def describe_table(self, table: str) -> dict[str, Any]:
        validate_identifier(table, "table name")
        if table.startswith("mv_"):
            try:
                return self._views.describe(table)
            except QueryError as e:
                return {"error": f"Materialized view '{table}' not found: {e}"}
        schema = self._catalog.get_table(table)
        return {
            "table": table,
            "columns": [c.as_dict() for c in schema.columns],
            "row_count": schema.row_count,
        }

    def query_data(self, sql: str) -> dict[str, Any]:
        return self._format_result(self._query.sql(sql))

    def search_data(
        self, table: str, filter_column: str, filter_value: str, limit: int = 20
    ) -> dict[str, Any]:
        validate_identifier(table, "table name")
        validate_identifier(filter_column, "column name")
        self._validate_columns_in_catalog(table, [filter_column])
        limit = min(max(1, int(limit)), MAX_RESULT_ROWS)
        filter_value = str(filter_value)

        pushed = self._try_search_pushdown(table, filter_column, filter_value, limit)
        if pushed is not None:
            return pushed

        operator = "LIKE" if "%" in filter_value else "="
        sql = (
            f"SELECT * FROM {table} WHERE CAST({filter_column} AS VARCHAR) {operator} ? "
            f"LIMIT {limit}"
        )
        return self._format_result(self._query.sql(sql, params=[filter_value]))

    def aggregate_data(
        self, table: str, group_by: str, agg_column: str, agg_func: str
    ) -> dict[str, Any]:
        validate_identifier(table, "table name")
        validate_identifier(group_by, "group_by column")
        validate_identifier(agg_column, "agg_column")
        self._validate_columns_in_catalog(table, [group_by, agg_column])

        func = str(agg_func).upper()
        if func not in ALLOWED_AGG_FUNCS:
            return {
                "error": f"Invalid aggregation function: {agg_func}. "
                f"Allowed: {sorted(ALLOWED_AGG_FUNCS)}"
            }
        alias = f"{func.lower()}_{agg_column}"

        pushed = self._try_aggregate_pushdown(table, group_by, agg_column, func, alias)
        if pushed is not None:
            return pushed

        sql = (
            f"SELECT {group_by}, {func}({agg_column}) AS {alias} FROM {table} "
            f"GROUP BY {group_by} ORDER BY {alias} DESC LIMIT {MAX_RESULT_ROWS}"
        )
        return self._format_result(self._query.sql(sql))

    def create_view(self, name: str, sql: str, refresh: str = "manual") -> dict[str, Any]:
        if not is_valid_view_name(name):
            return {"error": f"Invalid view name: '{name}'. Use alphanumeric and underscore only."}
        spec = self._views.create(name, sql, refresh=refresh)
        return {
            "status": "created",
            "name": spec.name,
            "table_name": spec.table_name,
            "refresh": spec.refresh,
        }

    def list_views(self) -> dict[str, Any]:
        return {"views": self._views.list_views()}

    def refresh_view(self, name: str) -> dict[str, Any]:
        self._views.refresh(name)
        return {"status": "refreshed", "name": name}

    def load_table(self, table: str) -> dict[str, Any]:
        validate_identifier(table, "table name")
        try:
            ref = TableRef.parse(table)
        except SchemaError:
            return {"error": "Use 'source.table' format (e.g. 'mydb.orders')"}
        if self._catalog.is_loaded(ref):
            return {"status": "already_loaded", "table": table}
        if not self._catalog.has_table(ref):
            return {"error": str(_missing_table_error(self._catalog, ref))}
        newly = self._sources.ensure_loaded([ref])
        if ref in newly:
            return {"status": "loaded", "table": table}
        return {"error": f"Failed to load table '{table}'"}

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    # -- helpers ------------------------------------------------------------

    def _validate_columns_in_catalog(self, table: str, columns: list[str]) -> None:
        """Defense-in-depth over the identifier regex: columns must exist.

        Materialized views live only in the store, so they are skipped.
        """
        if table.startswith("mv_"):
            return
        schema = self._catalog.get_table(table)  # SchemaError if unknown
        known = schema.column_names
        if not known:
            return  # schema could not be inferred; let the store reject bad columns
        for col in columns:
            if col not in known:
                raise QueryError(
                    f"Unknown column '{col}' in table '{table}'. "
                    f"Available columns: {', '.join(sorted(known))}"
                )

    def _pushdown_target(self, table: str) -> tuple[PushdownCapable | None, str]:
        """(source, bare table name) when pushdown applies, else (None, '')."""
        try:
            ref = TableRef.parse(table)
        except SchemaError:
            return None, ""
        if self._catalog.is_loaded(ref):
            return None, ""  # local is faster once loaded
        source = self._sources.pushdown_source(ref.source)
        if source is None:
            return None, ""
        return source, ref.table

    def _try_search_pushdown(
        self, table: str, column: str, value: str, limit: int
    ) -> dict[str, Any] | None:
        source, bare = self._pushdown_target(table)
        if source is None:
            return None
        try:
            started = time.perf_counter()
            if "%" in value:
                escaped = value.replace("'", "''")
                sql = (
                    f"SELECT * FROM {bare} WHERE CAST({column} AS VARCHAR) LIKE '{escaped}' "
                    f"LIMIT {limit}"
                )
                rows = source.execute_query(sql)
            else:
                rows = source.fetch_filtered(bare, {column: value}, limit=limit)
            return self._format_rowset(rows, (time.perf_counter() - started) * 1000)
        except Exception as e:
            logger.info("Search pushdown failed for %s, falling back: %s", table, e)
            return None

    def _try_aggregate_pushdown(
        self, table: str, group_by: str, agg_column: str, func: str, alias: str
    ) -> dict[str, Any] | None:
        source, bare = self._pushdown_target(table)
        if source is None:
            return None
        try:
            started = time.perf_counter()
            sql = (
                f"SELECT {group_by}, {func}({agg_column}) AS {alias} FROM {bare} "
                f"GROUP BY {group_by} ORDER BY {alias} DESC LIMIT {MAX_RESULT_ROWS}"
            )
            rows = source.execute_query(sql)
            return self._format_rowset(rows, (time.perf_counter() - started) * 1000)
        except Exception as e:
            logger.info("Aggregate pushdown failed for %s, falling back: %s", table, e)
            return None

    @staticmethod
    def _format_result(result: QueryResult) -> dict[str, Any]:
        records = result.to_records()
        truncated = len(records) > MAX_RESULT_ROWS
        return {
            "columns": list(result.columns),
            "rows": records[:MAX_RESULT_ROWS],
            "row_count": result.row_count,
            "truncated": truncated,
            "execution_time_ms": round(result.execution_time_ms, 1),
            "from_cache": result.from_cache,
        }

    @staticmethod
    def _format_rowset(rows: RowSet, elapsed_ms: float = 0.0) -> dict[str, Any]:
        records = rows.to_records()
        truncated = len(records) > MAX_RESULT_ROWS
        return {
            "columns": list(rows.columns),
            "rows": records[:MAX_RESULT_ROWS],
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": round(elapsed_ms, 1),
            "from_cache": False,
        }


def _missing_table_error(catalog: SchemaCatalog, ref: TableRef) -> SchemaError:
    try:
        catalog.get_table(ref)
    except SchemaError as e:
        return e
    return SchemaError(f"Table '{ref}' not found")
