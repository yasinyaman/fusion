"""MCP server exposing the 10 Fusion tools (stdio transport via ``run``)."""

from __future__ import annotations

import json
from typing import Any

from fusion.application.app import FusionApp


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


def create_mcp_server(fusion: FusionApp, name: str = "fusion") -> Any:
    """Build an ``MCPServer`` whose tools delegate to ``fusion.tools``.

    Every tool returns JSON text; failures come back as ``{"error": ...}``
    so the model gets a uniform, parseable shape.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)
    tools = fusion.tools

    @server.tool()
    def list_sources() -> str:
        """List all connected data sources and their tables with row counts."""
        return _dump(tools.execute("list_sources"))

    @server.tool()
    def describe_table(table: str) -> str:
        """Show detailed schema for a table (columns, types, row count).

        Use 'source.table' format.
        """
        return _dump(tools.execute("describe_table", {"table": table}))

    @server.tool()
    def query_data(sql: str) -> str:
        """Execute an analytical SQL query on DuckDB. Only SELECT queries allowed. Max 100 rows."""
        return _dump(tools.execute("query_data", {"sql": sql}))

    @server.tool()
    def search_data(table: str, filter_column: str, filter_value: str, limit: int = 20) -> str:
        """Search rows in a table matching a filter.

        Supports exact match or LIKE with % wildcards.
        """
        return _dump(
            tools.execute(
                "search_data",
                {
                    "table": table,
                    "filter_column": filter_column,
                    "filter_value": filter_value,
                    "limit": limit,
                },
            )
        )

    @server.tool()
    def aggregate_data(table: str, group_by: str, agg_column: str, agg_func: str) -> str:
        """Run GROUP BY aggregation. agg_func: SUM, AVG, COUNT, MIN, or MAX."""
        return _dump(
            tools.execute(
                "aggregate_data",
                {
                    "table": table,
                    "group_by": group_by,
                    "agg_column": agg_column,
                    "agg_func": agg_func,
                },
            )
        )

    @server.tool()
    def create_view(name: str, sql: str, refresh: str = "manual") -> str:
        """Create a materialized view (cached aggregation). refresh: 'manual', 'hourly', 'daily'."""
        return _dump(tools.execute("create_view", {"name": name, "sql": sql, "refresh": refresh}))

    @server.tool()
    def list_views() -> str:
        """List all materialized views with refresh schedule and last refresh time."""
        return _dump(tools.execute("list_views"))

    @server.tool()
    def refresh_view(name: str) -> str:
        """Manually refresh a materialized view to get latest data."""
        return _dump(tools.execute("refresh_view", {"name": name}))

    @server.tool()
    def load_table(table: str) -> str:
        """Load a specific table from its data source into DuckDB for querying."""
        return _dump(tools.execute("load_table", {"table": table}))

    @server.tool()
    def cache_stats() -> str:
        """Show query cache statistics: hit rate, entry count, memory usage."""
        return _dump(tools.execute("cache_stats"))

    return server
