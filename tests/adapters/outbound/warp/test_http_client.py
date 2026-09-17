"""Tests for the Warp HTTP transport: SSRF guard, pool + breaker, client URLs."""

import pytest
import responses

from fusion.adapters.outbound.warp.circuit_breaker import CircuitBreaker, CircuitState
from fusion.adapters.outbound.warp.connection_pool import ConnectionPool
from fusion.adapters.outbound.warp.http import (
    HttpTransportError,
    PooledHttpTransport,
    WarpHttpClient,
    build_transport,
    validate_base_url,
)
from fusion.domain.errors import CircuitOpenError, ConnectionError
from tests.fakes import FakeWarpTransport

BASE = "http://localhost:8080"


class TestSSRFGuard:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(ConnectionError):
            validate_base_url("file:///etc/passwd")
        with pytest.raises(ConnectionError):
            validate_base_url("ftp://example.com")

    def test_rejects_metadata_ip_and_hostname(self):
        with pytest.raises(ConnectionError):
            validate_base_url("http://169.254.169.254")
        with pytest.raises(ConnectionError):
            validate_base_url("http://metadata.google.internal/computeMetadata")

    def test_rejects_missing_host(self):
        with pytest.raises(ConnectionError):
            validate_base_url("http://")

    def test_allows_loopback_and_private(self):
        validate_base_url("http://localhost:8080")
        validate_base_url("http://127.0.0.1:8000")
        validate_base_url("http://10.0.0.5:8000")

    def test_client_validates_on_construction(self):
        with pytest.raises(ConnectionError):
            WarpHttpClient("http://169.254.169.254", "db", FakeWarpTransport({}))


class TestPooledHttpTransport:
    @responses.activate
    def test_get_json_and_post_json(self):
        responses.add(responses.GET, f"{BASE}/x", json={"ok": 1}, status=200)
        responses.add(responses.POST, f"{BASE}/y", json={"rows": []}, status=200)
        transport = PooledHttpTransport(ConnectionPool(max_retries=0))
        try:
            assert transport.get_json(f"{BASE}/x", params={"a": 1}) == {"ok": 1}
            assert transport.post_json(f"{BASE}/y", {"query": "SELECT 1"}) == {"rows": []}
            assert responses.calls[0].request.url.endswith("/x?a=1")
            assert b"SELECT 1" in responses.calls[1].request.body
        finally:
            transport.close()

    @responses.activate
    def test_http_error_becomes_transport_error_with_status(self):
        responses.add(responses.GET, f"{BASE}/x", json={}, status=503)
        transport = PooledHttpTransport(ConnectionPool(max_retries=0))
        with pytest.raises(HttpTransportError) as exc:
            transport.get_json(f"{BASE}/x")
        assert exc.value.status == 503
        transport.close()

    @responses.activate
    def test_invalid_json_is_transport_error(self):
        responses.add(responses.GET, f"{BASE}/x", body="not json", status=200)
        transport = PooledHttpTransport(ConnectionPool(max_retries=0))
        with pytest.raises(HttpTransportError, match="Invalid JSON"):
            transport.get_json(f"{BASE}/x")
        transport.close()

    @responses.activate
    def test_redirects_are_not_followed(self):
        responses.add(responses.GET, f"{BASE}/x", status=302, headers={"Location": "http://evil/"})
        transport = PooledHttpTransport(ConnectionPool(max_retries=0))
        with pytest.raises(HttpTransportError):
            transport.get_json(f"{BASE}/x")
        assert len(responses.calls) == 1
        transport.close()

    @responses.activate
    def test_breaker_opens_after_failures(self):
        responses.add(responses.GET, f"{BASE}/x", json={}, status=500)
        breaker = CircuitBreaker("t", failure_threshold=2, timeout=60)
        transport = PooledHttpTransport(ConnectionPool(max_retries=0), breaker)
        for _ in range(2):
            with pytest.raises(HttpTransportError):
                transport.get_json(f"{BASE}/x")
        assert breaker.state == CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            transport.get_json(f"{BASE}/x")
        assert len(responses.calls) == 2  # third call short-circuited
        transport.close()

    @responses.activate
    def test_bearer_token_sent(self):
        responses.add(responses.GET, f"{BASE}/x", json={}, status=200)
        transport = PooledHttpTransport(ConnectionPool(api_key="s3cr3t", max_retries=0))
        transport.get_json(f"{BASE}/x")
        assert responses.calls[0].request.headers["Authorization"] == "Bearer s3cr3t"
        transport.close()

    def test_build_transport_wires_pool_and_breaker(self):
        transport = build_transport(
            api_key="k", timeout=5, max_retries=1, circuit_breaker_threshold=3, breaker_name="b"
        )
        try:
            assert transport.breaker is not None
            assert transport.breaker.failure_threshold == 3
            assert transport.breaker.name == "b"
        finally:
            transport.close()


class TestWarpHttpClient:
    def test_builds_endpoint_urls(self):
        transport = FakeWarpTransport({"users": [{"id": 1}]}, database="db")
        client = WarpHttpClient(f"{BASE}/", "db", transport, timeout=3)
        assert client.base_url == BASE
        client.health()
        client.info()
        client.table_page("users", limit=5, offset=10)
        client.query("SELECT 1", params=[1])
        assert transport.urls("GET") == [
            f"{BASE}/health",
            f"{BASE}/info",
            f"{BASE}/api/v1/db/users",
        ]
        assert transport.requests[2][2] == {"limit": 5, "offset": 10}
        assert transport.requests[3] == (
            "POST",
            f"{BASE}/api/v1/db/query/execute",
            {"query": "SELECT 1", "params": [1]},
        )
        client.close()
        assert transport.closed
