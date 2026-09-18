"""ToolService: the 10 LLM tools, dispatched by name.

Every method returns a JSON-serializable dict; failures become
``{"error": "..."}`` so MCP/REST/SDK callers get one uniform shape.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from fusion.application.planner import FetchPlanner
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
from fusion.domain.models import MV_PREFIX, QueryResult, RowSet, TableRef
from fusion.domain.policy import TargetPlan
from fusion.domain.slices import Predicate, SliceSpec
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.data_source import PushdownCapable
from fusion.ports.sql_policy import SqlAnalyzer

_WHERE_HELP = (
    "load_table's 'where' must be an AND of simple conditions comparing a "
    "column to a literal (=, !=, <, <=, >, >=, LIKE, IN, IS NULL), for "
    "example \"status = 'paid' AND amount > 100\". OR, NOT, functions, "
    "subqueries and parameters cannot be sent to the source."
)

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
        planner: FetchPlanner | None = None,
        analyzer: SqlAnalyzer | None = None,
    ) -> None:
        self._query = query
        self._sources = sources
        self._views = views
        self._store = store
        self._catalog = catalog
        self._cache = cache
        self._planner = planner or query.planner
        self._analyzer = analyzer or query.analyzer
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
                slices = [s for s in self._catalog.slices_of(ref) if not s.is_full]
                tables.append(
                    {
                        "name": ref.full_name,
                        "row_count": row_count,
                        # What the source says the table holds, which is what
                        # decides whether it can be loaded whole at all.
                        "row_estimate": schema.row_estimate,
                        "columns": len(schema.columns),
                        "loaded": loaded,
                        "slices": [
                            {
                                "table": s.table_name,
                                "rows": s.row_count,
                                "where": s.spec.describe(),
                            }
                            for s in slices
                        ],
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

        # A table too big to load whole can still be searched: fetch just the
        # matching rows and read them from the slice.
        scanned = self._search_slice(table, filter_column, filter_value) or table
        operator = "LIKE" if "%" in filter_value else "="
        sql = (
            f"SELECT * FROM {scanned} WHERE CAST({filter_column} AS VARCHAR) {operator} ? "
            f"LIMIT {limit}"
        )
        return self._format_result(self._query.sql(sql, params=[filter_value]))

    def _search_slice(self, table: str, column: str, value: str) -> str | None:
        """Load the rows matching a search, when the table is not loaded yet.

        Only for text columns: the value arrives as a string, and a source
        that compares it literally would silently miss rows of a numeric or
        date column. Those fall back to loading the table.
        """
        if "." not in table or table.startswith(MV_PREFIX):
            return None
        ref = TableRef.parse(table)
        if self._catalog.is_loaded(ref) or not self._catalog.has_table(ref):
            return None
        schema = self._catalog.get_table(ref)
        if self._planner.policy.allows_full_load(schema.known_estimate):
            # Small enough to take whole, which serves every later query too.
            return None
        kinds = {c.name: c.type.lower() for c in schema.columns}
        if not any(kinds.get(column, "").startswith(text) for text in ("varchar", "text", "char")):
            return None
        source = self._sources.source(ref.source)
        if source is None or not source.capabilities.slices:
            return None
        spec = SliceSpec(predicates=(Predicate(column, "like" if "%" in value else "eq", value),))
        covering = self._catalog.find_covering_slice(ref, spec)
        if covering is not None:
            return covering.table_name
        try:
            return self._sources.ensure_slices([TargetPlan(ref, spec, "load_slice")]).get(ref)
        except QueryError as e:
            logger.info("Search slice for %s unavailable (%s); loading the table", table, e)
            return None

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

    def load_table(
        self,
        table: str,
        where: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load a table, or just the slice ``where``/``columns`` describe.

        Without ``where`` and ``columns`` this is the whole table, and a
        table too big for the configured limit is refused with advice. With
        them, only the matching rows and columns are fetched, which is how a
        table far larger than memory is still made queryable.
        """
        validate_identifier(table, "table name")
        try:
            ref = TableRef.parse(table)
        except SchemaError:
            return {"error": "Use 'source.table' format (e.g. 'mydb.orders')"}
        if not self._catalog.has_table(ref):
            return {"error": str(_missing_table_error(self._catalog, ref))}
        if where is None and not columns:
            return self._load_whole_table(ref, table)
        return self._load_slice(ref, table, where, columns)

    def _load_whole_table(self, ref: TableRef, table: str) -> dict[str, Any]:
        if self._catalog.is_loaded(ref):
            return {"status": "already_loaded", "table": table}
        plan = self._planner.plan_query(f"SELECT * FROM {table}")
        if plan.is_refused:
            return {"error": plan.refusal}
        newly = self._sources.ensure_loaded([ref])
        if ref in newly:
            return {"status": "loaded", "table": table, "row_count": self._row_count(ref)}
        return {"error": f"Failed to load table '{table}'"}

    def _load_slice(
        self, ref: TableRef, table: str, where: str | None, columns: list[str] | None
    ) -> dict[str, Any]:
        """Turn ``where``/``columns`` into a slice by parsing them as a SELECT."""
        if columns:
            for column in columns:
                validate_identifier(column, "column name")
            self._validate_columns_in_catalog(table, list(columns))
        projection = ", ".join(columns) if columns else "*"
        sql = f"SELECT {projection} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        shape = self._analyzer.analyze(sql)
        use = shape.use_for(ref) if shape.is_simple_select else None
        if use is None:
            return {"error": _WHERE_HELP}
        if where and not use.predicates:
            return {"error": _WHERE_HELP}
        spec = use.slice_spec()
        target = TargetPlan(ref, spec, "load_slice")
        try:
            table_name = self._sources.ensure_slices([target])[ref]
        except KeyError:
            return {"error": f"No connected source for '{table}'"}
        loaded = self._catalog.slices.get(table_name)
        return {
            "status": "loaded",
            "table": table,
            "slice": spec.describe(),
            "slice_table": table_name,
            "row_count": loaded.row_count if loaded else 0,
            "complete": loaded.complete if loaded else False,
        }

    def _row_count(self, ref: TableRef) -> int:
        try:
            return self._store.count(ref.full_name)
        except QueryError:
            return -1

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
