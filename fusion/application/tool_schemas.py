"""Tool schema definitions in OpenAI function-calling and MCP formats.

The 10 tools: list_sources, describe_table, query_data, search_data,
aggregate_data, create_view, list_views, refresh_view, load_table, cache_stats.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_sources",
        "description": (
            "List all connected data sources and their tables with row counts. "
            "Use this to discover what data is available before querying."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_table",
        "description": (
            "Show detailed schema for a table: column names, data types, "
            "nullability, and row count. Use format 'source.table'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format (e.g. 'mydb.orders')",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "query_data",
        "description": (
            "Execute an analytical SQL query on DuckDB. Only SELECT queries are allowed. "
            "Results are limited to 100 rows. Use this for complex joins, aggregations, "
            "window functions, and cross-source queries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A SELECT SQL query to execute on DuckDB"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "search_data",
        "description": (
            "Search for rows in a table matching a filter condition. "
            "Simpler than writing full SQL — just specify table, column, and value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format",
                },
                "filter_column": {"type": "string", "description": "Column name to filter on"},
                "filter_value": {
                    "type": "string",
                    "description": "Value to match (exact or LIKE pattern with %)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 20)",
                    "default": 20,
                },
            },
            "required": ["table", "filter_column", "filter_value"],
        },
    },
    {
        "name": "aggregate_data",
        "description": (
            "Run a GROUP BY aggregation on a table. Specify the grouping column, "
            "the column to aggregate, and the function (SUM, AVG, COUNT, MIN, MAX)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format",
                },
                "group_by": {"type": "string", "description": "Column to group by"},
                "agg_column": {"type": "string", "description": "Column to aggregate"},
                "agg_func": {
                    "type": "string",
                    "description": "Aggregation function: SUM, AVG, COUNT, MIN, or MAX",
                    "enum": ["SUM", "AVG", "COUNT", "MIN", "MAX"],
                },
            },
            "required": ["table", "group_by", "agg_column", "agg_func"],
        },
    },
    {
        "name": "create_view",
        "description": (
            "Create a materialized view (pre-computed table) from a SELECT query. "
            "Useful for caching expensive aggregations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "View name (alphanumeric and underscores only)",
                },
                "sql": {"type": "string", "description": "SELECT query to materialize"},
                "refresh": {
                    "type": "string",
                    "description": (
                        "Refresh interval: 'manual', 'hourly', 'daily', or 'every N minutes'"
                    ),
                    "default": "manual",
                },
            },
            "required": ["name", "sql"],
        },
    },
    {
        "name": "list_views",
        "description": (
            "List all materialized views with their refresh schedule and last refresh time."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "refresh_view",
        "description": "Manually refresh a materialized view to get the latest data.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the materialized view to refresh",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "load_table",
        "description": (
            "Load a table from its data source into DuckDB for querying. "
            "Use this if list_sources shows a table is not yet loaded. "
            "A table larger than the configured limit is refused unless you "
            "narrow it with 'where' and/or 'columns', which fetch only the "
            "matching rows and columns. After loading, the table is available "
            "to query_data and the other tools under its own name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format to load",
                },
                "where": {
                    "type": "string",
                    "description": (
                        "Optional filter sent to the source: an AND of simple "
                        "conditions comparing a column to a literal (=, !=, <, "
                        "<=, >, >=, LIKE, IN, IS NULL), e.g. "
                        "\"status = 'paid' AND created_at > '2026-01-01'\". "
                        "OR, NOT, functions and subqueries are not accepted."
                    ),
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of columns to fetch instead of all",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "cache_stats",
        "description": "Show query cache statistics: hit rate, entry count, and memory usage.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(d["name"] for d in TOOL_DEFINITIONS)


def get_openai_tools() -> list[dict[str, Any]]:
    """Definitions wrapped as OpenAI Chat Completions ``tools`` entries."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": deepcopy(d["parameters"]),
            },
        }
        for d in TOOL_DEFINITIONS
    ]


def get_mcp_tools() -> list[dict[str, Any]]:
    """Definitions in MCP ``name/description/inputSchema`` format."""
    return [
        {
            "name": d["name"],
            "description": d["description"],
            "inputSchema": deepcopy(d["parameters"]),
        }
        for d in TOOL_DEFINITIONS
    ]
