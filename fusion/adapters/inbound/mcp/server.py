"""MCP server exposing the 12 Fusion tools (stdio transport via ``run``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from fusion.application.app import FusionApp
from fusion.application.tool_schemas import TOOL_DEFINITIONS


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


def create_mcp_server(fusion: FusionApp, name: str = "fusion") -> Any:
    """Build an ``MCPServer`` whose tools delegate to ``fusion.tools``.

    Every tool returns JSON text; failures come back as ``{"error": ...}``
    so the model gets a uniform, parseable shape.
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.tools import Tool

    tools = fusion.tools
    definitions = {d["name"]: d for d in TOOL_DEFINITIONS}
    registered: list[Any] = []

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        """Register ``fn`` under the description and input schema an OpenAI client gets.

        The schema mcp derives from the signature has names and types only, so
        the parameter descriptions, the filter shape and its operators would
        never reach an MCP client. Arguments are still validated against the
        signature, which is why the two must name the same parameters.
        """
        definition = definitions[fn.__name__]
        derived = Tool.from_function(fn, description=definition["description"])
        published = definition["parameters"]
        if set(derived.parameters.get("properties", {})) != set(
            published.get("properties", {})
        ) or set(derived.parameters.get("required", [])) != set(published.get("required", [])):
            raise TypeError(f"MCP tool {fn.__name__!r} does not take the parameters it publishes")
        registered.append(derived.model_copy(update={"parameters": deepcopy(published)}))
        return fn

    @tool
    def list_sources() -> str:
        """List all connected data sources and their tables with row counts."""
        return _dump(tools.execute("list_sources"))

    @tool
    def describe_table(table: str) -> str:
        """Show detailed schema for a table (columns, types, row count).

        Use 'source.table' format.
        """
        return _dump(tools.execute("describe_table", {"table": table}))

    @tool
    def query_data(sql: str) -> str:
        """Execute an analytical SQL query on DuckDB. Only SELECT queries allowed. Max 100 rows."""
        return _dump(tools.execute("query_data", {"sql": sql}))

    @tool
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

    @tool
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

    @tool
    def create_view(name: str, sql: str, refresh: str = "manual") -> str:
        """Create a materialized view (cached aggregation). refresh: 'manual', 'hourly', 'daily'."""
        return _dump(tools.execute("create_view", {"name": name, "sql": sql, "refresh": refresh}))

    @tool
    def list_views() -> str:
        """List all materialized views with refresh schedule and last refresh time."""
        return _dump(tools.execute("list_views"))

    @tool
    def refresh_view(name: str) -> str:
        """Manually refresh a materialized view to get latest data."""
        return _dump(tools.execute("refresh_view", {"name": name}))

    @tool
    def load_table(table: str, where: str | None = None, columns: list[str] | None = None) -> str:
        """Load a table, or only the rows and columns ``where``/``columns`` describe."""
        return _dump(
            tools.execute("load_table", {"table": table, "where": where, "columns": columns})
        )

    @tool
    def list_metrics(table: str | None = None) -> str:
        """Measures, dimensions, grains and transforms available for a table."""
        return _dump(tools.execute("list_metrics", {"table": table}))

    @tool
    def query_metrics(
        table: str,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[dict] | None = None,
        order_by: str = "",
        limit: int = 100,
    ) -> str:
        """Answer a metric question without writing SQL (see list_metrics first)."""
        return _dump(
            tools.execute(
                "query_metrics",
                {
                    "table": table,
                    "metrics": metrics,
                    "dimensions": dimensions,
                    "filters": filters,
                    "order_by": order_by,
                    "limit": limit,
                },
            )
        )

    @tool
    def cache_stats() -> str:
        """Show query cache statistics: hit rate, entry count, memory usage."""
        return _dump(tools.execute("cache_stats"))

    return MCPServer(name, tools=registered)
