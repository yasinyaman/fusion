"""HTTP transport for Warp: SSRF guard, pool + circuit breaker, endpoint client."""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Any, Protocol
from urllib.parse import urlencode, urlparse

import requests

from fusion.adapters.outbound.warp.circuit_breaker import CircuitBreaker
from fusion.adapters.outbound.warp.connection_pool import ConnectionPool
from fusion.domain.errors import ConnectionError
from fusion.domain.slices import Predicate, SliceSpec

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_API_PREFIX = "/api/v1"
DEFAULT_API_KEY_HEADER = "X-API-Key"

#: Beyond this, a GET URL risks hitting a server or proxy limit, so the
#: request is sent as a POST with the same parameters in the body.
MAX_GET_URL_LENGTH = 6000
#: Long ``IN`` lists go in a POST body even when the URL would still fit.
MAX_GET_IN_VALUES = 50

# Cloud metadata hostnames that must never be contacted (SSRF targets).
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})


class HttpTransportError(ConnectionError):
    """An HTTP request to Warp failed (network error or non-2xx status)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_transport_failure(exc: BaseException) -> bool:
    """Whether an exception should count against the circuit breaker.

    A 4xx answer proves Warp is up and talking (a bad table name, a refused
    raw query, ...); only network errors, 5xx, 408 (timeout) and 429
    (overload) indicate the service itself is struggling.
    """
    if isinstance(exc, HttpTransportError) and exc.status is not None:
        status = exc.status
        return not (400 <= status < 500 and status not in (408, 429))
    return True


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


class ByteStream(Protocol):
    """An HTTP response whose body is read incrementally."""

    @property
    def raw(self) -> IO[bytes]:
        """File-like body (already content-decoded)."""
        ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_lines(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class HttpTransport(Protocol):
    """The HTTP calls the Warp client needs: JSON, and raw byte streams."""

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any: ...

    def post_json(
        self, url: str, payload: Mapping[str, Any], timeout: float | None = None
    ) -> Any: ...

    def get_stream(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> ByteStream:
        """GET whose body the caller reads (Arrow IPC, NDJSON, large JSON)."""
        ...

    def post_stream(
        self, url: str, payload: Mapping[str, Any], timeout: float | None = None
    ) -> ByteStream:
        """POST whose body the caller reads (same, for long request bodies)."""
        ...

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

    def get_stream(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> ByteStream:
        return self._stream("GET", url, params=dict(params or {}), timeout=timeout)

    def post_stream(
        self, url: str, payload: Mapping[str, Any], timeout: float | None = None
    ) -> ByteStream:
        return self._stream("POST", url, json=dict(payload), timeout=timeout)

    def _stream(self, method: str, url: str, **kwargs: Any) -> ByteStream:
        def do_request() -> requests.Response:
            return self._pool.stream(method, url, **kwargs)

        return self._call(do_request, url)

    def _guarded(self, method: Any, url: str, **kwargs: Any) -> Any:
        def do_request() -> Any:
            response = method(url, **kwargs)
            try:
                return response.json()
            except ValueError as e:
                raise HttpTransportError(f"Invalid JSON from {url}: {e}") from e

        return self._call(do_request, url)

    def _call(self, do_request: Any, url: str) -> Any:
        """Run a request through the breaker, mapping requests errors to ours."""

        def attempt() -> Any:
            try:
                return do_request()
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                raise HttpTransportError(f"HTTP {status} from {url}: {e}", status=status) from e
            except requests.RequestException as e:
                raise HttpTransportError(f"Request to {url} failed: {e}") from e

        if self._breaker is None:
            return attempt()
        return self._breaker.call(attempt)

    def close(self) -> None:
        self._pool.close()


class WarpHttpClient:
    """Typed access to the Warp endpoints Fusion uses.

    URL layout: ``{base_url}{api_prefix}/{database}/{table}`` (Warp >= 0.10
    always, and multi-database Warp 0.9). A single-database Warp 0.9 serves
    ``{api_prefix}/{table}`` only; ``configure(db_prefixed=False)`` switches
    to that layout once the source has probed it.
    """

    def __init__(
        self,
        base_url: str,
        database: str,
        transport: HttpTransport,
        timeout: float = DEFAULT_TIMEOUT,
        api_prefix: str = DEFAULT_API_PREFIX,
        db_prefixed: bool = True,
    ) -> None:
        validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self._transport = transport
        self.api_prefix = _normalize_prefix(api_prefix)
        self.db_prefixed = db_prefixed

    def configure(self, api_prefix: str | None = None, db_prefixed: bool | None = None) -> None:
        """Adopt the layout a running Warp reports (``/info``) or a probe found."""
        if api_prefix is not None:
            self.api_prefix = _normalize_prefix(api_prefix)
        if db_prefixed is not None:
            self.db_prefixed = db_prefixed

    @property
    def api_root(self) -> str:
        """``{base_url}{api_prefix}[/{database}]``: the root of every table URL."""
        root = f"{self.base_url}{self.api_prefix}"
        return f"{root}/{self.database}" if self.db_prefixed else root

    def table_url(self, table: str) -> str:
        return f"{self.api_root}/{table}"

    def health(self) -> Any:
        return self._transport.get_json(f"{self.base_url}/health", timeout=self.timeout)

    def info(self) -> Any:
        return self._transport.get_json(f"{self.base_url}/info", timeout=self.timeout)

    def table_page(
        self,
        table: str,
        limit: int,
        offset: int = 0,
        *,
        fields: Sequence[str] | None = None,
        filters: Sequence[Predicate] = (),
        sort: str | None = None,
    ) -> Any:
        """One page of a table, with the list endpoint's filters and projection."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if fields:
            params["fields"] = ",".join(fields)
        if sort:
            params["sort"] = sort
        params.update(filter_params(filters))
        return self._transport.get_json(self.table_url(table), params=params, timeout=self.timeout)

    def schema(self) -> Any:
        """Typed schema of every table (Warp >= 0.10)."""
        return self._transport.get_json(f"{self.api_root}/schema", timeout=self.timeout)

    def table_schema(self, table: str) -> Any:
        return self._transport.get_json(f"{self.table_url(table)}/schema", timeout=self.timeout)

    def export(
        self,
        table: str,
        spec: SliceSpec,
        fmt: str = "json",
        max_rows: int | None = None,
    ) -> ByteStream:
        """Stream a slice from ``/{table}/export``.

        Sent as a GET when the parameters fit comfortably in a URL, as a POST
        otherwise — a semi-join can carry thousands of key values.
        """
        url = f"{self.table_url(table)}/export"
        limit = spec.limit if max_rows is None else min(max_rows, spec.limit or max_rows)
        fields = sorted(spec.columns) if spec.columns is not None else None
        if _fits_in_url(url, spec, fields, fmt, limit):
            params: dict[str, Any] = {"format": fmt}
            if fields:
                params["fields"] = ",".join(fields)
            if limit is not None:
                params["limit"] = limit
            params.update(filter_params(spec.predicates))
            return self._transport.get_stream(url, params=params, timeout=self.timeout)
        payload: dict[str, Any] = {
            "format": fmt,
            "filters": [
                {"column": p.column, "op": p.op, "value": _wire_value(p)} for p in spec.predicates
            ],
        }
        if fields:
            payload["fields"] = fields
        if limit is not None:
            payload["limit"] = limit
        return self._transport.post_stream(url, payload, timeout=self.timeout)

    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """``POST .../query/execute``; ``params`` are Warp's named ``:name`` parameters."""
        payload: dict[str, Any] = {"query": sql}
        if params:
            payload["params"] = dict(params)
        return self._transport.post_json(
            f"{self.api_root}/query/execute", payload, timeout=self.timeout
        )

    def close(self) -> None:
        self._transport.close()


def _normalize_prefix(prefix: str) -> str:
    stripped = prefix.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def _wire_value(predicate: Predicate) -> Any:
    """JSON representation of a predicate value (tuples are not JSON)."""
    if predicate.op == "in":
        return list(predicate.value)
    return predicate.value


def filter_params(predicates: Sequence[Predicate]) -> dict[str, str]:
    """Encode predicates as Warp's ``filter[column][op]=value`` query parameters."""
    params: dict[str, str] = {}
    for predicate in predicates:
        key = f"filter[{predicate.column}][{predicate.op}]"
        if predicate.op == "in":
            params[key] = ",".join(_scalar_text(v) for v in predicate.value)
        elif predicate.op == "is_null":
            params[key] = "true" if predicate.value else "false"
        else:
            params[key] = _scalar_text(predicate.value)
    return params


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _fits_in_url(
    url: str,
    spec: SliceSpec,
    fields: Sequence[str] | None,
    fmt: str,
    limit: int | None,
) -> bool:
    """Whether this slice can be requested with a GET."""
    for predicate in spec.predicates:
        if predicate.op == "in" and len(predicate.value) > MAX_GET_IN_VALUES:
            return False
    params: dict[str, Any] = {"format": fmt}
    if fields:
        params["fields"] = ",".join(fields)
    if limit is not None:
        params["limit"] = limit
    params.update(filter_params(spec.predicates))
    return len(url) + 1 + len(urlencode(params)) <= MAX_GET_URL_LENGTH


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
    api_key_header: str = DEFAULT_API_KEY_HEADER,
) -> PooledHttpTransport:
    """Production transport: pooled session with retries behind a circuit breaker.

    The breaker counts only network errors, 5xx, 408 and 429 (see
    ``is_transport_failure``): a Warp answering 4xx is healthy.
    """
    pool = ConnectionPool(
        pool_size=pool_size,
        max_overflow=pool_max_overflow,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        timeout=timeout,
        api_key=api_key,
        api_key_header=api_key_header,
    )
    breaker = CircuitBreaker(
        breaker_name,
        failure_threshold=circuit_breaker_threshold,
        timeout=circuit_breaker_timeout,
        is_failure=is_transport_failure,
    )
    return PooledHttpTransport(pool, breaker)
