"""Tool executor that routes LLM tool calls to OLAPEngine operations."""

import logging
import re
from typing import Any

from fusion.exceptions import GuardrailViolation, QueryError, SchemaError
from fusion.result import QueryResult
from fusion.views.materialized import MaterializedViewManager

logger = logging.getLogger(__name__)

# Max rows returned to LLM to fit in context window
MAX_RESULT_ROWS = 100

# Allowed aggregation functions (whitelist for safety)
ALLOWED_AGG_FUNCS = {"SUM", "AVG", "COUNT", "MIN", "MAX"}

# Valid identifier pattern (prevents SQL injection in column/table names)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def _validate_identifier(name: str, label: str = "identifier") -> None:
    """Validate that a name is a safe SQL identifier."""
    if not _IDENTIFIER_RE.match(name):
        raise QueryError(f"Invalid {label}: '{name}'. Only alphanumeric, underscore, and dot allowed.")


class ToolExecutor:
    """Routes tool calls from LLMs to OLAPEngine operations.

    Each method returns a JSON-serializable dict suitable for returning
    to an LLM via MCP or OpenAI function calling.
    """

    def __init__(self, engine: Any):
        self._engine = engine
        self._mv = MaterializedViewManager(engine)

    def execute(self, tool_name: str, arguments: dict) -> dict:
        """Execute a tool by name with given arguments.

        Args:
            tool_name: One of the 10 defined tool names
            arguments: Dict of tool arguments

        Returns:
            JSON-serializable dict with the result or error
        """
        handler = getattr(self, tool_name, None)
        if handler is None or tool_name.startswith("_"):
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            return handler(**arguments)
        except (GuardrailViolation, QueryError, SchemaError) as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"Invalid arguments for {tool_name}: {e}"}
        except Exception as e:
            logger.exception("Tool execution error: %s", tool_name)
            return {"error": f"Internal error: {e}"}

    def list_sources(self) -> dict:
        """List all connected data sources, tables, and load status."""
        schemas = self._engine.catalog.get_all_schemas()

        sources = []
        for source_name, source_info in schemas.items():
            tables = []
            for table_name, table_meta in source_info["tables"].items():
                full_name = f"{source_name}.{table_name}"
                is_loaded = self._engine.catalog.is_loaded(full_name)
                row_count = table_meta.get("row_count", -1)

                # Get live count from DuckDB only for loaded tables
                if is_loaded:
                    try:
                        with self._engine._lock:
                            row_count = self._engine._conn.execute(
                                f"SELECT COUNT(*) FROM {full_name}"
                            ).fetchone()[0]
                    except Exception:
                        pass

                tables.append({
                    "name": full_name,
                    "row_count": row_count,
                    "columns": len(table_meta.get("columns", [])),
                    "loaded": is_loaded,
                })
            sources.append({
                "source": source_name,
                "type": source_info.get("type", "unknown"),
                "tables": tables,
            })

        return {"sources": sources}

    def describe_table(self, table: str) -> dict:
        """Show detailed schema for a specific table.

        Supports both 'source.table' format (catalog lookup) and
        materialized view names like 'mv_*' (introspected from DuckDB).
        """
        _validate_identifier(table, "table name")

        # Materialized views are DuckDB-local tables, not in the catalog
        if table.startswith("mv_"):
            return self._describe_mv(table)

        info = self._engine.catalog.get_table_info(table)
        return {
            "table": table,
            "columns": info.get("columns", []),
            "row_count": info.get("row_count", -1),
        }

    def query_data(self, sql: str) -> dict:
        """Execute a SQL query with guardrails, returning up to MAX_RESULT_ROWS."""
        result = self._engine.sql(sql)
        return self._format_result(result)

    def search_data(
        self,
        table: str,
        filter_column: str,
        filter_value: str,
        limit: int = 20,
    ) -> dict:
        """Search a table with a simple filter condition.

        If the table is not loaded in DuckDB and the connector supports
        pushdown, sends a filtered query directly to the source database.
        """
        _validate_identifier(table, "table name")
        _validate_identifier(filter_column, "column name")
        limit = min(max(1, limit), MAX_RESULT_ROWS)

        # Try pushdown: use connector.fetch_data_filtered if table not loaded
        pushdown_result = self._try_search_pushdown(
            table, filter_column, filter_value, limit
        )
        if pushdown_result is not None:
            return pushdown_result

        # Fallback: DuckDB path (auto-loads table if needed)
        if "%" in filter_value:
            sql = (
                f"SELECT * FROM {table} "
                f"WHERE CAST({filter_column} AS VARCHAR) LIKE '{self._escape_value(filter_value)}' "
                f"LIMIT {limit}"
            )
        else:
            sql = (
                f"SELECT * FROM {table} "
                f"WHERE CAST({filter_column} AS VARCHAR) = '{self._escape_value(filter_value)}' "
                f"LIMIT {limit}"
            )

        result = self._engine.sql(sql)
        return self._format_result(result)

    def aggregate_data(
        self,
        table: str,
        group_by: str,
        agg_column: str,
        agg_func: str,
    ) -> dict:
        """Run a GROUP BY aggregation on a table.

        If the table is not loaded in DuckDB and the connector supports
        pushdown, sends the aggregation query directly to the source database.
        """
        _validate_identifier(table, "table name")
        _validate_identifier(group_by, "group_by column")
        _validate_identifier(agg_column, "agg_column")

        agg_func_upper = agg_func.upper()
        if agg_func_upper not in ALLOWED_AGG_FUNCS:
            return {"error": f"Invalid aggregation function: {agg_func}. Allowed: {ALLOWED_AGG_FUNCS}"}

        # Try pushdown: send aggregation SQL to source connector
        pushdown_result = self._try_aggregate_pushdown(
            table, group_by, agg_column, agg_func_upper
        )
        if pushdown_result is not None:
            return pushdown_result

        # Fallback: DuckDB path (auto-loads table if needed)
        sql = (
            f"SELECT {group_by}, {agg_func_upper}({agg_column}) as {agg_func.lower()}_{agg_column} "
            f"FROM {table} "
            f"GROUP BY {group_by} "
            f"ORDER BY {agg_func.lower()}_{agg_column} DESC "
            f"LIMIT {MAX_RESULT_ROWS}"
        )

        result = self._engine.sql(sql)
        return self._format_result(result)

    def create_view(self, name: str, sql: str, refresh: str = "manual") -> dict:
        """Create a materialized view."""
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
            return {"error": f"Invalid view name: '{name}'. Use alphanumeric and underscore only."}

        self._mv.create(name, sql, refresh=refresh)
        return {
            "status": "created",
            "name": name,
            "table_name": f"mv_{name}",
            "refresh": refresh,
        }

    def list_views(self) -> dict:
        """List all materialized views."""
        views = self._mv.list_views()
        return {"views": views}

    def refresh_view(self, name: str) -> dict:
        """Refresh a materialized view."""
        self._mv.refresh(name)
        return {"status": "refreshed", "name": name}

    def load_table(self, table: str) -> dict:
        """Explicitly load a table from its source into DuckDB."""
        _validate_identifier(table, "table name")
        parts = table.split(".", 1)
        if len(parts) != 2:
            return {"error": "Use 'source.table' format (e.g. 'mydb.orders')"}

        if self._engine.catalog.is_loaded(table):
            return {"status": "already_loaded", "table": table}

        # Verify table exists in catalog
        try:
            self._engine.catalog.get_table_info(table)
        except SchemaError as e:
            return {"error": str(e)}

        newly = self._engine.ensure_tables_loaded([table])
        if table in newly:
            return {"status": "loaded", "table": table}
        return {"error": f"Failed to load table '{table}'"}

    def cache_stats(self) -> dict:
        """Return query cache statistics."""
        return self._engine.cache_stats()

    def _format_result(self, result: QueryResult) -> dict:
        """Format a QueryResult for LLM consumption."""
        rows = result.to_dict()
        truncated = False

        if len(rows) > MAX_RESULT_ROWS:
            rows = rows[:MAX_RESULT_ROWS]
            truncated = True

        return {
            "columns": result.columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": truncated,
            "execution_time_ms": round(result.execution_time, 1),
            "from_cache": result.from_cache,
        }

    def _describe_mv(self, table: str) -> dict:
        """Describe a materialized view by introspecting DuckDB directly."""
        try:
            with self._engine._lock:
                # Get column info from PRAGMA
                result = self._engine._conn.execute(
                    f"PRAGMA table_info('{table}')"
                ).fetchall()

            if not result:
                return {"error": f"Materialized view '{table}' not found"}

            columns = []
            for row in result:
                # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
                columns.append({
                    "name": row[1],
                    "type": row[2],
                    "nullable": not row[3],
                })

            # Get row count
            with self._engine._lock:
                row_count = self._engine._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]

            return {
                "table": table,
                "columns": columns,
                "row_count": row_count,
            }
        except Exception as e:
            return {"error": f"Materialized view '{table}' not found: {e}"}

    def _get_pushdown_connector(self, table: str):
        """Get a pushdown-capable connector for a table, or None.

        Returns the connector only if:
        - Table is in 'source.table' format
        - Table is NOT loaded in DuckDB
        - A connector exists for the source
        - The connector supports pushdown
        """
        parts = table.split(".", 1)
        if len(parts) != 2:
            return None, None

        source_name, table_name = parts

        # Skip if already loaded in DuckDB (local is faster)
        if self._engine.catalog.is_loaded(table):
            return None, None

        connector = self._engine._connectors.get(source_name)
        if connector is None or not connector.supports_pushdown:
            return None, None

        return connector, table_name

    def _try_search_pushdown(
        self,
        table: str,
        filter_column: str,
        filter_value: str,
        limit: int,
    ) -> dict | None:
        """Attempt search via connector pushdown. Returns None if not applicable."""
        connector, table_name = self._get_pushdown_connector(table)
        if connector is None:
            return None

        try:
            if "%" in filter_value:
                # LIKE filter — use execute_query for full SQL control
                escaped = self._escape_value(filter_value)
                sql = (
                    f"SELECT * FROM {table_name} "
                    f"WHERE CAST({filter_column} AS VARCHAR) LIKE '{escaped}' "
                    f"LIMIT {limit}"
                )
                df = connector.execute_query(sql)
            else:
                # Exact match — use fetch_data_filtered
                df = connector.fetch_data_filtered(
                    table_name,
                    filters={filter_column: filter_value},
                    limit=limit,
                )

            return self._format_dataframe_result(df)
        except Exception as e:
            logger.info("Search pushdown failed for %s, falling back: %s", table, e)
            return None

    def _try_aggregate_pushdown(
        self,
        table: str,
        group_by: str,
        agg_column: str,
        agg_func: str,
    ) -> dict | None:
        """Attempt aggregation via connector pushdown. Returns None if not applicable."""
        connector, table_name = self._get_pushdown_connector(table)
        if connector is None:
            return None

        try:
            sql = (
                f"SELECT {group_by}, {agg_func}({agg_column}) as {agg_func.lower()}_{agg_column} "
                f"FROM {table_name} "
                f"GROUP BY {group_by} "
                f"ORDER BY {agg_func.lower()}_{agg_column} DESC "
                f"LIMIT {MAX_RESULT_ROWS}"
            )
            df = connector.execute_query(sql)
            return self._format_dataframe_result(df)
        except Exception as e:
            logger.info("Aggregate pushdown failed for %s, falling back: %s", table, e)
            return None

    @staticmethod
    def _format_dataframe_result(df) -> dict:
        """Format a pandas DataFrame as a tool result dict."""
        columns = list(df.columns) if not df.empty else []
        rows = df.to_dict(orient="records") if not df.empty else []

        truncated = False
        if len(rows) > MAX_RESULT_ROWS:
            rows = rows[:MAX_RESULT_ROWS]
            truncated = True

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(df),
            "truncated": truncated,
            "execution_time_ms": 0.0,
            "from_cache": False,
        }

    @staticmethod
    def _escape_value(value: str) -> str:
        """Escape single quotes in a string value for safe SQL embedding."""
        return value.replace("'", "''")
