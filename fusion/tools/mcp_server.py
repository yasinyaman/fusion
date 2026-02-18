"""MCP Server for Fusion — exposes analytics tools via Model Context Protocol.

Run as CLI:
    fusion-mcp --warp-url http://localhost:8080 --database mydb

Configure in Claude Desktop claude_desktop_config.json:
    {
        "mcpServers": {
            "fusion": {
                "command": "fusion-mcp",
                "args": ["--warp-url", "http://localhost:8080", "--database", "mydb"]
            }
        }
    }
"""

import argparse
import json
import logging
import sys

logger = logging.getLogger(__name__)

# Lazy imports to avoid hard dependency on mcp package
_executor = None
_engine = None


def _get_executor():
    global _executor
    return _executor


def _create_mcp_app():
    """Create the FastMCP application with all tool handlers."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("fusion")

    @mcp.tool()
    def list_sources() -> str:
        """List all connected data sources and their tables with row counts."""
        return json.dumps(_get_executor().list_sources(), default=str)

    @mcp.tool()
    def describe_table(table: str) -> str:
        """Show detailed schema for a table (columns, types, row count). Use 'source.table' format."""
        return json.dumps(_get_executor().describe_table(table), default=str)

    @mcp.tool()
    def query_data(sql: str) -> str:
        """Execute an analytical SQL query on DuckDB. Only SELECT queries allowed. Max 100 rows."""
        return json.dumps(_get_executor().query_data(sql), default=str)

    @mcp.tool()
    def search_data(table: str, filter_column: str, filter_value: str, limit: int = 20) -> str:
        """Search rows in a table matching a filter. Supports exact match or LIKE with % wildcards."""
        return json.dumps(
            _get_executor().search_data(table, filter_column, filter_value, limit),
            default=str,
        )

    @mcp.tool()
    def aggregate_data(table: str, group_by: str, agg_column: str, agg_func: str) -> str:
        """Run GROUP BY aggregation. agg_func: SUM, AVG, COUNT, MIN, or MAX."""
        return json.dumps(
            _get_executor().aggregate_data(table, group_by, agg_column, agg_func),
            default=str,
        )

    @mcp.tool()
    def create_view(name: str, sql: str, refresh: str = "manual") -> str:
        """Create a materialized view (cached aggregation). refresh: 'manual', 'hourly', 'daily'."""
        return json.dumps(_get_executor().create_view(name, sql, refresh), default=str)

    @mcp.tool()
    def list_views() -> str:
        """List all materialized views with refresh schedule and last refresh time."""
        return json.dumps(_get_executor().list_views(), default=str)

    @mcp.tool()
    def refresh_view(name: str) -> str:
        """Manually refresh a materialized view to get latest data."""
        return json.dumps(_get_executor().refresh_view(name), default=str)

    @mcp.tool()
    def load_table(table: str) -> str:
        """Load a specific table from its data source into DuckDB for querying."""
        return json.dumps(_get_executor().load_table(table), default=str)

    @mcp.tool()
    def cache_stats() -> str:
        """Show query cache statistics: hit rate, entry count, memory usage."""
        return json.dumps(_get_executor().cache_stats(), default=str)

    return mcp


def main():
    """CLI entry point for fusion-mcp server."""
    parser = argparse.ArgumentParser(
        description="Fusion MCP Server — DuckDB analytics tools for LLMs",
    )
    parser.add_argument(
        "--warp-url",
        default="http://localhost:8080",
        help="Warp REST API base URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--database",
        default="primary_db",
        help="Database name to connect via Warp (default: primary_db)",
    )
    parser.add_argument(
        "--memory-limit",
        default="4GB",
        help="DuckDB memory limit (default: 4GB)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="DuckDB thread count (default: 4)",
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        help=(
            "Auto-discover all databases from Warp and connect each as a "
            "separate source. When used, --database is ignored."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Additional source in 'name=X,url=Y,db=Z' format. "
            "Can be repeated for multi-source federation."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    from fusion.engine import OLAPEngine
    from fusion.tools.executor import ToolExecutor

    global _executor, _engine

    _engine = OLAPEngine(
        memory_limit=args.memory_limit,
        threads=args.threads,
    )

    # Connect Warp source(s)
    if args.auto_discover:
        from fusion.connectors.warp import WarpConnector

        databases = WarpConnector.discover_databases(args.warp_url)
        if not databases:
            logger.warning(
                "No databases discovered from %s, falling back to --database",
                args.warp_url,
            )
            databases = [args.database]
        for db_name in databases:
            _engine.connect_source(db_name, {
                "type": "warp",
                "base_url": args.warp_url,
                "database": db_name,
            })
        logger.info(
            "Auto-discovered %d databases from %s: %s",
            len(databases), args.warp_url, databases,
        )
    else:
        _engine.connect_source(args.database, {
            "type": "warp",
            "base_url": args.warp_url,
            "database": args.database,
        })

    # Connect additional sources (--source name=X,url=Y,db=Z)
    for source_str in args.source:
        parts = dict(p.split("=", 1) for p in source_str.split(",") if "=" in p)
        src_name = parts.get("name", "")
        src_url = parts.get("url", "")
        src_db = parts.get("db", src_name)
        if src_name and src_url:
            _engine.connect_source(src_name, {
                "type": "warp",
                "base_url": src_url,
                "database": src_db,
            })

    _executor = ToolExecutor(_engine)

    logger.info(
        "Starting Fusion MCP server (warp=%s, db=%s, extra_sources=%d)",
        args.warp_url, args.database, len(args.source),
    )

    mcp = _create_mcp_app()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
