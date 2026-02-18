"""Tool schema definitions in OpenAI and MCP formats.

Defines 10 tools that LLMs can invoke to interact with Fusion:
list_sources, describe_table, query_data, search_data, aggregate_data,
create_view, list_views, refresh_view, load_table, cache_stats.
"""

from copy import deepcopy

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_sources",
        "description": (
            "List all connected data sources and their tables with row counts. "
            "Use this to discover what data is available before querying."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
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
                "sql": {
                    "type": "string",
                    "description": "A SELECT SQL query to execute on DuckDB",
                },
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
                "filter_column": {
                    "type": "string",
                    "description": "Column name to filter on",
                },
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
                "group_by": {
                    "type": "string",
                    "description": "Column to group by",
                },
                "agg_column": {
                    "type": "string",
                    "description": "Column to aggregate",
                },
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
                "sql": {
                    "type": "string",
                    "description": "SELECT query to materialize",
                },
                "refresh": {
                    "type": "string",
                    "description": "Refresh interval: 'manual', 'hourly', 'daily', or 'every N minutes'",
                    "default": "manual",
                },
            },
            "required": ["name", "sql"],
        },
    },
    {
        "name": "list_views",
        "description": "List all materialized views with their refresh schedule and last refresh time.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
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
            "Load a specific table from its data source into DuckDB for querying. "
            "Use this if list_sources shows a table is not yet loaded. "
            "After loading, the table is available for query_data and other tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Full table name in 'source.table' format to load",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "cache_stats",
        "description": "Show query cache statistics: hit rate, entry count, and memory usage.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def get_openai_tools() -> list[dict]:
    """Return tool definitions in OpenAI function calling format.

    Returns a list of dicts with "type": "function" wrapper as expected
    by the OpenAI Chat Completions API tools parameter.
    """
    tools = []
    for defn in TOOL_DEFINITIONS:
        tools.append({
            "type": "function",
            "function": {
                "name": defn["name"],
                "description": defn["description"],
                "parameters": deepcopy(defn["parameters"]),
            },
        })
    return tools


def get_mcp_tools() -> list[dict]:
    """Return tool definitions in MCP (Model Context Protocol) format.

    Returns a list of dicts with "name", "description", and "inputSchema"
    as expected by the MCP tool registration protocol.
    """
    tools = []
    for defn in TOOL_DEFINITIONS:
        tools.append({
            "name": defn["name"],
            "description": defn["description"],
            "inputSchema": deepcopy(defn["parameters"]),
        })
    return tools
