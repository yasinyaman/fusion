"""Discover the databases a Warp instance exposes (before creating sources)."""

from __future__ import annotations

import logging
from typing import Any

from fusion.adapters.outbound.warp.connection_pool import ConnectionPool
from fusion.adapters.outbound.warp.http import (
    DEFAULT_API_KEY_HEADER,
    DEFAULT_TIMEOUT,
    HttpTransport,
    PooledHttpTransport,
    validate_base_url,
)
from fusion.domain.errors import ConnectionError

logger = logging.getLogger(__name__)


def extract_database_names(info: Any) -> list[str]:
    databases: list[str] = []
    if isinstance(info, dict) and "databases" in info:
        dbs = info["databases"]
        if isinstance(dbs, dict):  # Format A: {"db": {...}}
            databases.extend(dbs.keys())
        elif isinstance(dbs, list):  # Format B: [{"name": ...}] or ["db", ...]
            for db in dbs:
                if isinstance(db, dict) and "name" in db:
                    databases.append(db["name"])
                elif isinstance(db, str):
                    databases.append(db)
    return databases


def discover_databases(
    base_url: str,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: HttpTransport | None = None,
    api_key_header: str = DEFAULT_API_KEY_HEADER,
) -> list[str]:
    """Database names from ``/info`` (empty when the payload has none)."""
    base_url = base_url.rstrip("/")
    validate_base_url(base_url)
    owned = transport is None
    if transport is None:
        transport = PooledHttpTransport(
            ConnectionPool(api_key=api_key, api_key_header=api_key_header, timeout=timeout)
        )
    try:
        try:
            transport.get_json(f"{base_url}/health", timeout=timeout)
        except ConnectionError as e:
            raise ConnectionError(f"Cannot connect to Warp at {base_url}: {e}") from e
        try:
            info = transport.get_json(f"{base_url}/info", timeout=timeout)
        except ConnectionError as e:
            raise ConnectionError(f"Failed to discover databases from Warp: {e}") from e
    finally:
        if owned:
            transport.close()
    databases = extract_database_names(info)
    logger.info("Discovered %d databases at %s: %s", len(databases), base_url, databases)
    return databases


class WarpDiscovery:
    """DatabaseDiscovery port implementation.

    ``api_key`` / ``api_key_header`` given here are the defaults used when a
    call does not pass its own key (the CLIs build one from ``Settings``).
    """

    def __init__(
        self,
        transport: HttpTransport | None = None,
        api_key: str | None = None,
        api_key_header: str = DEFAULT_API_KEY_HEADER,
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._api_key_header = api_key_header

    def discover_databases(
        self, base_url: str, api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> list[str]:
        return discover_databases(
            base_url,
            api_key if api_key is not None else self._api_key,
            timeout,
            transport=self._transport,
            api_key_header=self._api_key_header,
        )
