"""Main OLAP engine orchestrating DuckDB, caching, guardrails, and connectors."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fusion.tools.executor import ToolExecutor

import duckdb

from fusion.cache import QueryCache
from fusion.catalog import SchemaCatalog
from fusion.exceptions import ConnectionError, QueryError
from fusion.guardrails import SQLGuardrails
from fusion.result import QueryResult

logger = logging.getLogger(__name__)


class OLAPEngine:
    """Main Fusion OLAP engine backed by DuckDB.

    Orchestrates data source connections, SQL execution with guardrails,
    query caching, and schema catalog management.
    """

    def __init__(
        self,
        database: str = ":memory:",
        threads: int = 4,
        memory_limit: str = "4GB",
        cache_max_entries: int = 500,
        cache_ttl: int = 300,
        enable_external_access: bool = False,
        max_temp_directory_size: Optional[str] = None,
        max_ingest_rows: int = 0,
    ):
        self._conn = duckdb.connect(database)
        self._conn.execute(f"SET threads TO {threads}")
        self._conn.execute(f"SET memory_limit = '{memory_limit}'")

        # Cap on-disk spill so a runaway query can't fill the disk.
        if max_temp_directory_size:
            self._conn.execute(
                f"SET max_temp_directory_size = '{max_temp_directory_size}'"
            )
        # Max rows to pull from a source when materializing a table (0 = no cap).
        self._max_ingest_rows = max_ingest_rows

        # Security: disable DuckDB's access to the local filesystem and network
        # by default. This blocks read_csv/read_parquet/glob/ATTACH/COPY against
        # files and httpfs URLs — the main way a crafted SELECT could exfiltrate
        # local files or trigger SSRF. It is a one-way latch and cannot be
        # re-enabled at runtime. Enable only if you need EXPORT DATABASE backups.
        self._external_access = enable_external_access
        if not enable_external_access:
            self._conn.execute("SET enable_external_access = FALSE")

        self._lock = threading.Lock()

        self.cache = QueryCache(max_entries=cache_max_entries, default_ttl=cache_ttl)
        self.catalog = SchemaCatalog()
        self.guardrails = SQLGuardrails()

        self._connectors: dict[str, Any] = {}
        self._auto_refresh_timer: Optional[threading.Timer] = None
        self._auto_refresh_running = False

        logger.info(
            "OLAPEngine initialized (db=%s, threads=%d, memory=%s)",
            database, threads, memory_limit,
        )

    def connect_source(self, name: str, config: dict, fetch_all: bool = False) -> None:
        """Connect a data source and register its metadata.

        By default only fetches schema metadata (table names, columns, types).
        Data is loaded on-demand when queries reference specific tables.

        Args:
            name: Unique source name (used as schema prefix, e.g. "warp_main")
            config: Source configuration dict with "type" key and connection params
            fetch_all: If True, eagerly fetch all table data (backward compat)
        """
        from fusion.connectors import create_connector

        source_type = config.get("type")
        if not source_type:
            raise ConnectionError("Config must include 'type' key")

        connector = create_connector(name, config)
        connector.connect()

        # Create schema in DuckDB
        with self._lock:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")

        # Discover schema metadata (lightweight — samples 5 rows per table)
        schema_info = connector.get_schema()
        tables_meta = {}

        for table_name, table_schema in schema_info.items():
            tables_meta[table_name] = {
                "columns": table_schema.get("columns", []),
                "row_count": table_schema.get("row_count", -1),
            }

        # Register metadata in catalog
        self.catalog.register_source(name, source_type, tables_meta)
        self._connectors[name] = connector
        logger.info(
            "Connected source: %s (%d tables, metadata only)",
            name, len(tables_meta),
        )

        # Optionally load all data eagerly
        if fetch_all:
            all_tables = [f"{name}.{t}" for t in tables_meta]
            self.ensure_tables_loaded(all_tables)

    def ensure_tables_loaded(self, tables: list[str]) -> list[str]:
        """Ensure a list of 'source.table' strings are materialized in DuckDB.

        Returns list of tables that were newly fetched.
        """
        newly_loaded = []
        for full_name in tables:
            if self.catalog.is_loaded(full_name):
                continue
            parts = full_name.split(".", 1)
            if len(parts) != 2:
                continue
            source_name, table_name = parts
            if source_name not in self._connectors:
                logger.warning(
                    "No connector for source '%s', cannot load %s",
                    source_name, full_name,
                )
                continue
            self._fetch_table_on_demand(source_name, table_name)
            newly_loaded.append(full_name)
        return newly_loaded

    def _fetch_table_on_demand(self, source_name: str, table_name: str) -> None:
        """Fetch a single table from its source and materialize it in DuckDB."""
        connector = self._connectors[source_name]
        full_name = f"{source_name}.{table_name}"
        logger.info("On-demand loading: %s", full_name)

        df = connector.fetch_data(table_name, max_rows=self._max_ingest_rows or None)
        self._materialize_dataframe(full_name, df)

        # Update catalog row count
        row_count = len(df)
        try:
            source_schema = self.catalog.get_schema(source_name)
            if table_name in source_schema["tables"]:
                source_schema["tables"][table_name]["row_count"] = row_count
        except Exception:
            pass

        self.catalog.mark_loaded(full_name)
        logger.info("On-demand load complete: %s (%d rows)", full_name, row_count)

    def _materialize_dataframe(self, full_name: str, df: Any) -> None:
        """Materialize a pandas DataFrame as a DuckDB table.

        Uses an explicit register/unregister cycle instead of a replacement
        scan (``SELECT * FROM df``). Replacement scans of Python locals require
        DuckDB external access, which is disabled by default for security, so the
        registration path is what keeps table loading working under that latch.
        """
        tmp = "_fusion_df_ingest"
        with self._lock:
            self._conn.register(tmp, df)
            try:
                self._conn.execute(
                    f"CREATE OR REPLACE TABLE {full_name} AS SELECT * FROM {tmp}"
                )
            finally:
                self._conn.unregister(tmp)

    def disconnect_source(self, name: str) -> None:
        """Disconnect a data source and remove its data from DuckDB."""
        if name in self._connectors:
            self._connectors[name].close()
            del self._connectors[name]

        with self._lock:
            self._conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")

        self.catalog.unregister_source(name)
        logger.info("Disconnected source: %s", name)

    def sql(
        self,
        query: str,
        use_cache: bool = True,
        cache_ttl: Optional[int] = None,
        auto_load: bool = True,
        params: Optional[list] = None,
    ) -> QueryResult:
        """Execute a SQL query with guardrails and optional caching.

        Args:
            query: SQL query string (must be a SELECT/CTE query)
            use_cache: Whether to use query cache
            cache_ttl: Cache TTL in seconds (None = default)
            auto_load: If True, auto-detect and load referenced tables on demand
            params: Values to bind to ``?`` placeholders in the query. When
                provided, the query always runs on DuckDB (pushdown is skipped,
                since ``?`` binding is DuckDB-specific).
        """
        # Validate through guardrails
        self.guardrails.validate(query)

        # Check cache first (before any loading or pushdown)
        if use_cache:
            cached = self.cache.get(query, params)
            if cached is not None:
                logger.debug("Cache hit for query: %s", query[:80])
                return QueryResult(
                    data=cached._data,
                    columns=cached._columns,
                    sql=cached.sql,
                    execution_time=0.0,
                    from_cache=True,
                )

        # Auto-load referenced tables if connectors are available
        if auto_load and self._connectors:
            from fusion.strategy import FetchStrategy

            strategy = FetchStrategy(self.catalog)
            plan = strategy.plan_for_sql(query)

            # Try pushdown: send query to source database directly.
            # Skipped for parameterized queries — ``?`` placeholders are bound by
            # DuckDB and may not match the source database's binding style.
            if params is None and plan.pushdown_eligible and plan.source_name:
                connector = self._connectors.get(plan.source_name)
                if connector and connector.supports_pushdown:
                    try:
                        result = self._execute_pushdown(query, plan, connector)
                        # Cache pushdown results too
                        if use_cache:
                            self.cache.put(query, result, ttl=cache_ttl)
                        return result
                    except Exception as e:
                        logger.info(
                            "Pushdown failed, falling back to DuckDB: %s", e
                        )

            # Fallback: load tables into DuckDB
            if not plan.is_empty():
                self.ensure_tables_loaded([t.full_name for t in plan.targets])

        # Execute query on DuckDB
        try:
            start = time.perf_counter()
            with self._lock:
                if params is not None:
                    cursor = self._conn.execute(query, params)
                else:
                    cursor = self._conn.execute(query)
                columns = [desc[0] for desc in cursor.description]
                data = cursor.fetchall()
            elapsed_ms = (time.perf_counter() - start) * 1000
        except duckdb.Error as e:
            raise QueryError(f"Query execution failed: {e}") from e

        query_result = QueryResult(
            data=data,
            columns=columns,
            sql=query,
            execution_time=elapsed_ms,
            from_cache=False,
        )

        # Store in cache
        if use_cache:
            self.cache.put(query, query_result, ttl=cache_ttl, params=params)

        logger.debug("Query executed in %.1fms (%d rows)", elapsed_ms, len(data))
        return query_result

    def _execute_pushdown(
        self, query: str, plan: Any, connector: Any
    ) -> QueryResult:
        """Execute a query via pushdown to the source connector.

        Rewrites the SQL to remove source prefixes, sends it to the
        connector's backend database, and wraps the result as QueryResult.
        """
        rewritten = self._rewrite_sql_for_pushdown(query, plan)
        logger.info("Pushdown query to %s: %s", plan.source_name, rewritten[:120])

        start = time.perf_counter()
        df = connector.execute_query(rewritten)
        elapsed_ms = (time.perf_counter() - start) * 1000

        columns = list(df.columns) if not df.empty else []
        data = [tuple(row) for row in df.itertuples(index=False, name=None)]

        return QueryResult(
            data=data,
            columns=columns,
            sql=query,
            execution_time=elapsed_ms,
            from_cache=False,
        )

    @staticmethod
    def _rewrite_sql_for_pushdown(sql: str, plan: Any) -> str:
        """Remove source schema prefixes from SQL for pushdown execution.

        E.g. 'SELECT * FROM warp_ecommerce.orders' -> 'SELECT * FROM orders'
        """
        import sqlglot

        try:
            parsed = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.IGNORE)
            if not parsed:
                return sql

            source_prefix = plan.source_name
            for statement in parsed:
                if statement is None:
                    continue
                for table_node in statement.find_all(sqlglot.exp.Table):
                    if table_node.db and table_node.db == source_prefix:
                        table_node.set("db", None)

            return parsed[0].sql() if parsed[0] else sql
        except Exception:
            # Fallback: simple string replacement
            if plan.source_name:
                return sql.replace(f"{plan.source_name}.", "")
            return sql

    def refresh_sources(self, force: bool = False) -> None:
        """Refresh data from all connected sources (only reloads loaded tables)."""
        for name, connector in list(self._connectors.items()):
            try:
                schema_info = connector.get_schema()
                tables_meta = {}

                for table_name, table_schema in schema_info.items():
                    full_name = f"{name}.{table_name}"
                    row_count = table_schema.get("row_count", -1)

                    # Only re-fetch tables that are already loaded
                    if self.catalog.is_loaded(full_name):
                        df = connector.fetch_data(
                            table_name, max_rows=self._max_ingest_rows or None
                        )
                        self._materialize_dataframe(full_name, df)
                        row_count = len(df)

                    tables_meta[table_name] = {
                        "columns": table_schema.get("columns", []),
                        "row_count": row_count,
                    }

                self.catalog.register_source(
                    name, self.catalog.get_schema(name)["type"], tables_meta
                )
                logger.info("Refreshed source: %s", name)
            except Exception as e:
                logger.error("Failed to refresh source %s: %s", name, e)
                if force:
                    raise

    def start_auto_refresh(self, interval: int = 300) -> None:
        """Start automatic source refresh on a timer (interval in seconds)."""
        self._auto_refresh_running = True

        def _refresh_loop():
            if self._auto_refresh_running:
                try:
                    self.refresh_sources()
                except Exception as e:
                    logger.error("Auto-refresh failed: %s", e)
                self._auto_refresh_timer = threading.Timer(interval, _refresh_loop)
                self._auto_refresh_timer.daemon = True
                self._auto_refresh_timer.start()

        self._auto_refresh_timer = threading.Timer(interval, _refresh_loop)
        self._auto_refresh_timer.daemon = True
        self._auto_refresh_timer.start()
        logger.info("Auto-refresh started (every %ds)", interval)

    def stop_auto_refresh(self) -> None:
        """Stop automatic source refresh."""
        self._auto_refresh_running = False
        if self._auto_refresh_timer:
            self._auto_refresh_timer.cancel()
            self._auto_refresh_timer = None
        logger.info("Auto-refresh stopped")

    def cache_stats(self) -> dict:
        """Return cache statistics."""
        return self.cache.stats()

    def schema_context(self, schemas: Optional[list[str]] = None) -> str:
        """Generate LLM-friendly schema context string."""
        return self.catalog.generate_context(schemas)

    def table_stats(self) -> dict:
        """Return row counts for all tables (-1 for unloaded tables)."""
        result = {}
        for table in self.catalog.list_tables():
            if not self.catalog.is_loaded(table):
                result[table] = -1
                continue
            try:
                with self._lock:
                    row = self._conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()
                result[table] = row[0] if row else -1
            except Exception:
                result[table] = -1
        return result

    def execute_raw(self, sql: str) -> Any:
        """Execute raw SQL without guardrails (for internal use only)."""
        with self._lock:
            return self._conn.execute(sql)

    def close(self) -> None:
        """Close engine: stop auto-refresh, disconnect sources, close DuckDB."""
        self.stop_auto_refresh()
        for name in list(self._connectors):
            try:
                self._connectors[name].close()
            except Exception:
                pass
        self._connectors.clear()
        self._conn.close()
        logger.info("OLAPEngine closed")

    def get_tool_executor(self) -> "ToolExecutor":
        """Create and return a ToolExecutor for this engine."""
        from fusion.tools.executor import ToolExecutor
        return ToolExecutor(self)

    def as_openai_tools(self) -> list[dict]:
        """Return tool definitions in OpenAI function calling format."""
        from fusion.tools.definitions import get_openai_tools
        return get_openai_tools()

    def as_mcp_tools(self) -> list[dict]:
        """Return tool definitions in MCP format."""
        from fusion.tools.definitions import get_mcp_tools
        return get_mcp_tools()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
