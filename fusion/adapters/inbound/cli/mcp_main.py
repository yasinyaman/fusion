"""``fusion-mcp``: serve the tools over MCP (stdio).

    fusion-mcp --warp-url http://localhost:8000 --database mydb

Claude Desktop (``claude_desktop_config.json``)::

    {"mcpServers": {"fusion": {"command": "fusion-mcp",
                               "args": ["--warp-url", "http://localhost:8000",
                                        "--database", "mydb"]}}}
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace

from fusion import __version__
from fusion.adapters.inbound.cli.common import add_source_args, connect_sources
from fusion.application.settings import Settings
from fusion.bootstrap import build_app
from fusion.domain.errors import FusionError
from fusion.observability.logging import setup_logging

logger = logging.getLogger(__name__)


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fusion-mcp",
        description="Fusion MCP Server — DuckDB analytics tools for LLMs",
    )
    add_source_args(parser, settings.warp_url)
    parser.add_argument(
        "--memory-limit",
        default=settings.memory_limit,
        help=f"DuckDB memory limit (default: {settings.memory_limit})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=settings.threads,
        help=f"DuckDB thread count (default: {settings.threads})",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: WARNING); logs go to stderr",
    )
    parser.add_argument("--version", action="version", version=f"fusion {__version__}")
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    base = Settings.from_env(environ)
    args = build_parser(base).parse_args(argv)
    settings = replace(
        base,
        memory_limit=args.memory_limit,
        threads=args.threads,
        log_level=args.log_level,
        log_format="text",
        log_file="",
    )
    # stdout carries the MCP protocol; every log line must go to stderr.
    setup_logging(settings, stream=sys.stderr)

    fusion = build_app(settings)
    try:
        try:
            connected = connect_sources(
                fusion, args.warp_url, args.database, args.auto_discover, args.source
            )
        except (FusionError, ValueError) as e:
            logger.error("Failed to connect data sources: %s", e)
            return 1
        logger.info(
            "Starting Fusion MCP server v%s (warp=%s, sources=%s)",
            __version__,
            args.warp_url,
            connected,
        )

        from fusion.adapters.inbound.mcp.server import create_mcp_server

        create_mcp_server(fusion).run(transport="stdio")
        return 0
    finally:
        fusion.close()
