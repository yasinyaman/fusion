"""Shared CLI plumbing: source arguments and Warp source wiring."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from fusion.application.app import FusionApp
from fusion.bootstrap import default_discovery
from fusion.ports.data_source import DatabaseDiscovery

logger = logging.getLogger(__name__)


def add_source_args(parser: argparse.ArgumentParser, warp_url_default: str) -> None:
    parser.add_argument(
        "--warp-url",
        default=warp_url_default,
        help=f"Warp REST API base URL (default: {warp_url_default}, from WARP_URL)",
    )
    parser.add_argument(
        "--database",
        default="primary_db",
        help="Database name to connect via Warp (default: primary_db)",
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
        metavar="SPEC",
        help=(
            "Additional source in 'name=X,url=Y,db=Z' format. "
            "Can be repeated for multi-source federation."
        ),
    )


def parse_source_spec(spec: str) -> dict[str, str]:
    """``'name=X,url=Y,db=Z'`` -> ``{"name": X, "url": Y, "db": Z}`` (db defaults to name)."""
    parts = dict(p.split("=", 1) for p in spec.split(",") if "=" in p)
    name = parts.get("name", "").strip()
    url = parts.get("url", "").strip()
    if not name or not url:
        raise ValueError(f"Invalid --source '{spec}': expected name=X,url=Y[,db=Z]")
    return {"name": name, "url": url, "db": parts.get("db", name).strip() or name}


def connect_sources(
    fusion: FusionApp,
    warp_url: str,
    database: str,
    auto_discover: bool = False,
    extra: Sequence[str] = (),
    discovery: DatabaseDiscovery | None = None,
) -> list[str]:
    """Connect the Warp source(s) described by the CLI flags; returns their names."""
    connected: list[str] = []

    if auto_discover:
        databases = (discovery or default_discovery(fusion.settings)).discover_databases(warp_url)
        if not databases:
            logger.warning("No databases discovered from %s, falling back to --database", warp_url)
            databases = [database]
        for db_name in databases:
            fusion.sources.connect(
                db_name, {"type": "warp", "base_url": warp_url, "database": db_name}
            )
            connected.append(db_name)
        logger.info("Auto-discovered %d databases from %s: %s", len(databases), warp_url, databases)
    else:
        fusion.sources.connect(
            database, {"type": "warp", "base_url": warp_url, "database": database}
        )
        connected.append(database)

    for spec in extra:
        parsed = parse_source_spec(spec)
        fusion.sources.connect(
            parsed["name"],
            {"type": "warp", "base_url": parsed["url"], "database": parsed["db"]},
        )
        connected.append(parsed["name"])

    return connected
