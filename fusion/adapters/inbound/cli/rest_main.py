"""``fusion-rest``: serve the REST API.

fusion-rest --warp-url http://localhost:8000 --auto-discover --port 9000
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence

from fusion import __version__
from fusion.adapters.inbound.cli.common import add_source_args, connect_sources
from fusion.application.settings import Settings
from fusion.bootstrap import build_app
from fusion.domain.errors import ConnectionError, FusionError
from fusion.observability.logging import setup_logging

logger = logging.getLogger(__name__)


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fusion-rest",
        description="Fusion REST API Server — DuckDB analytics via HTTP",
    )
    parser.add_argument(
        "--host", default=settings.host, help=f"Bind host (default: {settings.host})"
    )
    parser.add_argument(
        "--port", type=int, default=settings.port, help=f"Bind port (default: {settings.port})"
    )
    add_source_args(parser, settings.warp_url)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    parser.add_argument("--version", action="version", version=f"fusion {__version__}")
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    settings = Settings.from_env(environ)
    args = build_parser(settings).parse_args(argv)

    errors = settings.validate()
    if errors:
        for err in errors:
            logging.getLogger(__name__).error("Configuration error: %s", err)
        logging.getLogger(__name__).error("Refusing to start. Fix the above and retry.")
        return 1

    setup_logging(settings)
    if settings.backup_enabled and not settings.external_access:
        logger.warning(
            "Backups are enabled but FUSION_DUCKDB_EXTERNAL_ACCESS is false; "
            "in-memory EXPORT DATABASE backups will fail. Set "
            "FUSION_DUCKDB_EXTERNAL_ACCESS=true to allow them (widens the attack surface)."
        )

    fusion = build_app(settings)
    try:
        try:
            connected = connect_sources(
                fusion, args.warp_url, args.database, args.auto_discover, args.source
            )
        except (ConnectionError, ValueError, FusionError) as e:
            logger.error("Failed to connect data sources: %s", e)
            return 1

        from fusion.adapters.inbound.rest.app import create_app

        app = create_app(fusion, settings)

        logger.info("Starting Fusion REST API Server v%s", __version__)
        logger.info("  Environment: %s", settings.env)
        logger.info("  Host: %s:%s", args.host, args.port)
        logger.info("  Warp URL: %s", args.warp_url)
        logger.info("  Sources: %s", ", ".join(connected))
        logger.info("  Auth: %s", "enabled" if settings.requires_auth() else "disabled")
        logger.info("  Rate limit: %s", settings.rate_limit)
        logger.info("  CORS: %s", list(settings.cors_origins))
        logger.info("  Backup: %s", "enabled" if settings.backup_enabled else "disabled")
        logger.info("  Docs: http://%s:%s/docs", args.host, args.port)

        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(
                app=app, host=args.host, port=args.port, reload=args.reload, log_config=None
            )
        )
        server.run()  # handles SIGINT/SIGTERM and returns on shutdown
        return 0
    finally:
        fusion.close()
