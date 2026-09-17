"""HTTP transport for Warp: SSRF guard, pool + circuit breaker, endpoint client."""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from fusion.adapters.outbound.warp.circuit_breaker import CircuitBreaker
from fusion.adapters.outbound.warp.connection_pool import ConnectionPool
from fusion.domain.errors import ConnectionError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

# Cloud metadata hostnames that must never be contacted (SSRF targets).
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})


class HttpTransportError(ConnectionError):
    """An HTTP request to Warp failed (network error or non-2xx status)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _is_blocked_ip(host: str) -> bool:
    """True for link-local / cloud-metadata addresses (e.g. 169.254.169.254).

    Loopback and private ranges are intentionally allowed: Warp normally runs
    on localhost or a private Docker network.
    """
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False


def validate_base_url(url: str) -> None:
    """Reject non-http(s) schemes and cloud-metadata / link-local hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConnectionError(
            f"Unsupported Warp URL scheme '{parsed.scheme}'. Only http/https are allowed: {url}"
        )
    host = parsed.hostname
    if not host:
        raise ConnectionError(f"Invalid Warp URL (no host): {url}")
    if host.lower() in _BLOCKED_HOSTNAMES or _is_blocked_ip(host):
        raise ConnectionError(f"Blocked Warp host '{host}' (cloud metadata / link-local address).")
    try:
        resolved = socket.gethostbyname(host)
    except OSError:
        resolved = None
    if resolved and _is_blocked_ip(resolved):
        raise ConnectionError(
            f"Blocked Warp host '{host}' (resolves to link-local address {resolved})."
        )


class HttpTransport(Protocol):
    """Minimal JSON-over-HTTP transport the Warp client needs."""

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any: ...

    def post_json(
        self, url: str, payload: Mapping[str, Any], timeout: float | None = None
    ) -> Any: ...

    def close(self) -> None: ...


class PooledHttpTransport:
    """HttpTransport over ConnectionPool (retries) guarded by a CircuitBreaker."""

    def __init__(self, pool: ConnectionPool, breaker: CircuitBreaker | None = None) -> None:
        self._pool = pool
        self._breaker = breaker

    @property
    def breaker(self) -> CircuitBreaker | None:
        return self._breaker

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        return self._guarded(self._pool.get, url, params=dict(params or {}), timeout=timeout)

    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float | None = None) -> Any:
        return self._guarded(self._pool.post, url, json=dict(payload), timeout=timeout)

    def _guarded(self, method: Any, url: str, **kwargs: Any) -> Any:
        def do_request() -> Any:
            try:
                response = method(url, **kwargs)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                raise HttpTransportError(f"HTTP {status} from {url}: {e}", status=status) from e
            except requests.RequestException as e:
                raise HttpTransportError(f"Request to {url} failed: {e}") from e
            try:
                return response.json()
            except ValueError as e:
                raise HttpTransportError(f"Invalid JSON from {url}: {e}") from e

        if self._breaker is None:
            return do_request()
        return self._breaker.call(do_request)

    def close(self) -> None:
        self._pool.close()


class WarpHttpClient:
    """Typed access to the Warp endpoints Fusion uses."""

    def __init__(
        self,
        base_url: str,
        database: str,
        transport: HttpTransport,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self._transport = transport

    def health(self) -> Any:
        return self._transport.get_json(f"{self.base_url}/health", timeout=self.timeout)

    def info(self) -> Any:
        return self._transport.get_json(f"{self.base_url}/info", timeout=self.timeout)

    def table_page(self, table: str, limit: int, offset: int = 0) -> Any:
        return self._transport.get_json(
            f"{self.base_url}/api/v1/{self.database}/{table}",
            params={"limit": limit, "offset": offset},
            timeout=self.timeout,
        )

    def query(self, sql: str, params: list[Any] | None = None) -> Any:
        payload: dict[str, Any] = {"query": sql}
        if params:
            payload["params"] = params
        return self._transport.post_json(
            f"{self.base_url}/api/v1/{self.database}/query/execute",
            payload,
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._transport.close()


def build_transport(
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
    pool_size: int = 10,
    pool_max_overflow: int = 5,
    circuit_breaker_threshold: int = 5,
    circuit_breaker_timeout: float = 60.0,
    breaker_name: str = "warp",
) -> PooledHttpTransport:
    """Production transport: pooled session with retries behind a circuit breaker."""
    pool = ConnectionPool(
        pool_size=pool_size,
        max_overflow=pool_max_overflow,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        timeout=timeout,
        api_key=api_key,
    )
    breaker = CircuitBreaker(
        breaker_name, failure_threshold=circuit_breaker_threshold, timeout=circuit_breaker_timeout
    )
    return PooledHttpTransport(pool, breaker)
