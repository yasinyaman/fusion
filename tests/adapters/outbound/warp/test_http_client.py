"""Tests for the Warp HTTP transport: SSRF guard, pool + breaker, client URLs."""

import pytest
import responses

from fusion.adapters.outbound.warp.circuit_breaker import CircuitBreaker, CircuitState
from fusion.adapters.outbound.warp.connection_pool import ConnectionPool
from fusion.adapters.outbound.warp.http import (
    MAX_GET_IN_VALUES,
    HttpTransportError,
    PooledHttpTransport,
    WarpHttpClient,
    build_transport,
    filter_params,
    is_transport_failure,
    validate_base_url,
)
from fusion.domain.errors import CircuitOpenError, ConnectionError
from fusion.domain.slices import Predicate, SliceSpec
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
    def test_api_key_header_sent(self):
        responses.add(responses.GET, f"{BASE}/x", json={}, status=200)
        transport = PooledHttpTransport(ConnectionPool(api_key="s3cr3t", max_retries=0))
        transport.get_json(f"{BASE}/x")
        headers = responses.calls[0].request.headers
        assert headers["X-API-Key"] == "s3cr3t"
        assert "Authorization" not in headers
        transport.close()

    @responses.activate
    def test_breaker_does_not_count_4xx(self):
        responses.add(responses.GET, f"{BASE}/missing", json={}, status=404)
        responses.add(responses.POST, f"{BASE}/query", json={}, status=403)
        transport = build_transport(max_retries=0, circuit_breaker_threshold=1)
        try:
            for _ in range(3):
                with pytest.raises(HttpTransportError):
                    transport.get_json(f"{BASE}/missing")
            with pytest.raises(HttpTransportError):
                transport.post_json(f"{BASE}/query", {"query": "SELECT 1"})
            assert transport.breaker is not None
            assert transport.breaker.state == CircuitState.CLOSED
            assert transport.breaker.failure_count == 0
        finally:
            transport.close()

    @responses.activate
    def test_breaker_counts_429_and_5xx(self):
        responses.add(responses.GET, f"{BASE}/busy", json={}, status=429)
        transport = build_transport(max_retries=0, circuit_breaker_threshold=1)
        try:
            with pytest.raises(HttpTransportError):
                transport.get_json(f"{BASE}/busy")
            assert transport.breaker is not None
            assert transport.breaker.state == CircuitState.OPEN
        finally:
            transport.close()

    def test_is_transport_failure_predicate(self):
        assert is_transport_failure(HttpTransportError("x", status=500))
        assert is_transport_failure(HttpTransportError("x", status=408))
        assert is_transport_failure(HttpTransportError("x", status=429))
        assert is_transport_failure(HttpTransportError("network"))  # no status
        assert is_transport_failure(RuntimeError("anything else"))
        assert not is_transport_failure(HttpTransportError("x", status=400))
        assert not is_transport_failure(HttpTransportError("x", status=403))
        assert not is_transport_failure(HttpTransportError("x", status=404))

    def test_build_transport_wires_pool_and_breaker(self):
        transport = build_transport(
            api_key="k",
            api_key_header="X-Token",
            timeout=5,
            max_retries=1,
            circuit_breaker_threshold=3,
            breaker_name="b",
        )
        try:
            assert transport.breaker is not None
            assert transport.breaker.failure_threshold == 3
            assert transport.breaker.name == "b"
            assert transport.breaker.is_failure is is_transport_failure
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
        client.query("SELECT 1 WHERE x = :x", params={"x": 1})
        assert transport.urls("GET") == [
            f"{BASE}/health",
            f"{BASE}/info",
            f"{BASE}/api/v1/db/users",
        ]
        assert transport.requests[2][2] == {"limit": 5, "offset": 10}
        # Warp binds named ``:name`` parameters from an object, not a list.
        assert transport.requests[3] == (
            "POST",
            f"{BASE}/api/v1/db/query/execute",
            {"query": "SELECT 1 WHERE x = :x", "params": {"x": 1}},
        )
        client.close()
        assert transport.closed

    def test_query_without_params_omits_key(self):
        transport = FakeWarpTransport({}, database="db")
        WarpHttpClient(BASE, "db", transport).query("SELECT 1", params={})
        assert transport.requests[-1][2] == {"query": "SELECT 1"}

    def test_layout_follows_api_prefix_and_db_prefix(self):
        transport = FakeWarpTransport({"users": [{"id": 1}]}, database="db")
        client = WarpHttpClient(BASE, "db", transport, api_prefix="v2/", db_prefixed=False)
        assert client.api_prefix == "/v2"
        assert client.table_url("users") == f"{BASE}/v2/users"
        client.configure(api_prefix="/api/v1", db_prefixed=True)
        assert client.table_url("users") == f"{BASE}/api/v1/db/users"
        client.configure(db_prefixed=False)
        assert client.api_root == f"{BASE}/api/v1"
        client.configure(api_prefix="")
        assert client.table_url("users") == f"{BASE}/users"


class TestFilterParams:
    def test_encodes_every_operator(self):
        params = filter_params(
            [
                Predicate("status", "eq", "new"),
                Predicate("total", "gte", 10),
                Predicate("id", "in", (1, 2, 3)),
                Predicate("note", "is_null", True),
                Predicate("flag", "eq", False),
                Predicate("name", "like", "%a%"),
            ]
        )
        assert params == {
            "filter[status][eq]": "new",
            "filter[total][gte]": "10",
            "filter[id][in]": "1,2,3",
            "filter[note][is_null]": "true",
            "filter[flag][eq]": "false",
            "filter[name][like]": "%a%",
        }

    def test_no_predicates(self):
        assert filter_params([]) == {}


class TestExport:
    def _client(self):
        transport = FakeWarpTransport({"orders": [{"id": 1, "s": "new"}]}, database="db")
        return WarpHttpClient(BASE, "db", transport), transport

    def test_get_for_a_small_slice(self):
        client, transport = self._client()
        spec = SliceSpec(columns=frozenset({"id"}), predicates=(Predicate("s", "eq", "new"),))
        client.export("orders", spec, fmt="ndjson")
        method, url, params = transport.requests[-1]
        assert method == "GET"
        assert url == f"{BASE}/api/v1/db/orders/export"
        assert params == {"format": "ndjson", "fields": "id", "filter[s][eq]": "new"}

    def test_limit_combines_the_spec_and_the_caller(self):
        client, transport = self._client()
        client.export("orders", SliceSpec(limit=50), fmt="json", max_rows=10)
        assert transport.requests[-1][2]["limit"] == 10
        client.export("orders", SliceSpec(limit=5), fmt="json", max_rows=10)
        assert transport.requests[-1][2]["limit"] == 5
        client.export("orders", SliceSpec.FULL, fmt="json")
        assert "limit" not in transport.requests[-1][2]

    def test_post_for_a_long_in_list(self):
        client, transport = self._client()
        keys = tuple(range(MAX_GET_IN_VALUES + 1))
        spec = SliceSpec(predicates=(Predicate("id", "in", keys),))
        client.export("orders", spec, fmt="arrow")
        method, url, payload = transport.requests[-1]
        assert method == "POST"
        assert url.endswith("/orders/export")
        assert payload["filters"] == [{"column": "id", "op": "in", "value": list(keys)}]
        assert payload["format"] == "arrow"

    def test_short_in_list_still_fits_in_a_get(self):
        client, transport = self._client()
        spec = SliceSpec(predicates=(Predicate("id", "in", (1, 2, 3)),))
        client.export("orders", spec, fmt="json")
        assert transport.requests[-1][0] == "GET"

    def test_post_when_the_url_would_be_too_long(self):
        client, transport = self._client()
        spec = SliceSpec(predicates=tuple(Predicate(f"c{i}", "eq", "x" * 200) for i in range(40)))
        client.export("orders", spec, fmt="json")
        assert transport.requests[-1][0] == "POST"
        assert len(transport.requests[-1][2]["filters"]) == 40


class TestSchemaEndpoints:
    def test_urls(self):
        transport = FakeWarpTransport({"orders": [{"id": 1}]}, database="db")
        client = WarpHttpClient(BASE, "db", transport)
        client.schema()
        client.table_schema("orders")
        assert transport.urls("GET")[-2:] == [
            f"{BASE}/api/v1/db/schema",
            f"{BASE}/api/v1/db/orders/schema",
        ]

    def test_table_page_carries_fields_filters_and_sort(self):
        transport = FakeWarpTransport({"orders": [{"id": 1, "s": "new"}]}, database="db")
        client = WarpHttpClient(BASE, "db", transport)
        client.table_page(
            "orders",
            limit=10,
            fields=["id"],
            filters=[Predicate("s", "eq", "new")],
            sort="id:asc",
        )
        assert transport.requests[-1][2] == {
            "limit": 10,
            "offset": 0,
            "fields": "id",
            "sort": "id:asc",
            "filter[s][eq]": "new",
        }
