"""Tests for Warp database discovery."""

import pytest
import responses

from fusion.adapters.outbound.warp.discovery import (
    WarpDiscovery,
    discover_databases,
    extract_database_names,
)
from fusion.adapters.outbound.warp.http import HttpTransportError
from fusion.domain.errors import ConnectionError
from tests.fakes import FakeWarpTransport

BASE = "http://localhost:8080"


class TestExtractDatabaseNames:
    def test_formats(self):
        assert extract_database_names({"databases": {"a": {}, "b": {}}}) == ["a", "b"]
        assert extract_database_names({"databases": [{"name": "a"}, "b"]}) == ["a", "b"]
        assert extract_database_names({"tables": ["t"]}) == []
        assert extract_database_names("junk") == []


class TestDiscoverDatabases:
    def test_with_injected_transport(self):
        transport = FakeWarpTransport({}, info={"databases": {"mydb": {}, "analytics": {}}})
        assert discover_databases(BASE, transport=transport) == ["mydb", "analytics"]
        assert transport.urls("GET") == [f"{BASE}/health", f"{BASE}/info"]
        assert not transport.closed  # caller owns injected transports

    def test_connection_error(self):
        transport = FakeWarpTransport({}, health_ok=False)
        with pytest.raises(ConnectionError, match="Cannot connect"):
            discover_databases(BASE, transport=transport)

    def test_info_error(self):
        transport = FakeWarpTransport({})
        original = transport.get_json

        def flaky(url, params=None, timeout=None):  # health ok, /info fails
            if url.endswith("/info"):
                raise HttpTransportError("HTTP 500", status=500)
            return original(url, params, timeout)

        transport.get_json = flaky  # type: ignore[method-assign]
        with pytest.raises(ConnectionError, match="discover databases"):
            discover_databases(BASE, transport=transport)

    def test_rejects_metadata_host(self):
        with pytest.raises(ConnectionError):
            discover_databases("http://169.254.169.254")

    @responses.activate
    def test_real_http_contract(self):
        responses.add(responses.GET, f"{BASE}/health", json={}, status=200)
        responses.add(
            responses.GET, f"{BASE}/info", json={"databases": {"db1": {}, "db2": {}}}, status=200
        )
        assert set(discover_databases(BASE, api_key="k")) == {"db1", "db2"}
        assert responses.calls[0].request.headers["Authorization"] == "Bearer k"

    def test_warp_discovery_port(self):
        transport = FakeWarpTransport({}, info={"databases": ["x"]})
        assert WarpDiscovery(transport).discover_databases(BASE) == ["x"]
