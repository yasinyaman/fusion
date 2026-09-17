"""HTTP-contract tests for WarpSource using the `responses` library.

These exercise the real requests flow (URL building, query params, status
handling, request bodies) so drift in the Warp REST API contract is caught.
"""

import pytest
import responses

from fusion.adapters.outbound.warp.source import WarpSource
from fusion.domain.errors import ConnectionError, QueryError

BASE = "http://localhost:8080"


def _make_source(**overrides):
    cfg = {"type": "warp", "base_url": BASE, "database": "mydb", "max_retries": 0}
    cfg.update(overrides)
    return WarpSource.from_config("warp", cfg)


def _stub_connect(tables=("users", "orders")):
    responses.add(responses.GET, f"{BASE}/health", json={"status": "ok"}, status=200)
    responses.add(
        responses.GET,
        f"{BASE}/info",
        json={"databases": {"mydb": {"tables": list(tables)}}},
        status=200,
    )


class TestWarpSourceContract:
    @responses.activate
    def test_connect_discovers_tables(self):
        _stub_connect()
        source = _make_source()
        source.connect()
        assert source.is_connected
        assert set(source.tables) == {"users", "orders"}
        source.close()

    @responses.activate
    def test_health_500_raises_connection_error(self):
        responses.add(responses.GET, f"{BASE}/health", json={"err": "x"}, status=500)
        with pytest.raises(ConnectionError):
            _make_source().connect()

    @responses.activate
    def test_fetch_table_paginates(self):
        _stub_connect(("t",))
        responses.add(responses.GET, f"{BASE}/api/v1/mydb/t", json={"data": [{"id": 1}, {"id": 2}]})
        responses.add(responses.GET, f"{BASE}/api/v1/mydb/t", json={"data": [{"id": 3}]})
        source = _make_source(page_size=2)
        source.connect()
        assert source.fetch_table("t").column("id") == [1, 2, 3]
        assert "limit=2&offset=0" in responses.calls[2].request.url
        assert "limit=2&offset=2" in responses.calls[3].request.url

    @responses.activate
    def test_fetch_table_respects_max_rows(self):
        _stub_connect(("t",))
        responses.add(responses.GET, f"{BASE}/api/v1/mydb/t", json={"data": [{"id": 1}, {"id": 2}]})
        source = _make_source(page_size=10)
        source.connect()
        assert len(source.fetch_table("t", max_rows=1)) == 1
        assert "limit=1" in responses.calls[2].request.url

    @responses.activate
    def test_execute_query_sends_sql_in_body(self):
        _stub_connect(("t",))
        responses.add(
            responses.POST, f"{BASE}/api/v1/mydb/query/execute", json={"data": [{"cnt": 42}]}
        )
        source = _make_source()
        source.connect()
        assert source.execute_query("SELECT COUNT(*) AS cnt FROM t").to_records() == [{"cnt": 42}]
        assert b"SELECT" in responses.calls[-1].request.body

    @responses.activate
    def test_execute_query_500_raises_query_error(self):
        _stub_connect(("t",))
        responses.add(
            responses.POST, f"{BASE}/api/v1/mydb/query/execute", json={"error": "bad"}, status=500
        )
        source = _make_source()
        source.connect()
        with pytest.raises(QueryError):
            source.execute_query("SELECT 1")

    @responses.activate
    def test_bearer_auth_header_sent(self):
        _stub_connect(())
        source = _make_source(api_key="s3cr3t")
        source.connect()
        assert responses.calls[0].request.headers["Authorization"] == "Bearer s3cr3t"

    @responses.activate
    def test_discover_schema_infers_types(self):
        _stub_connect(("t",))
        responses.add(
            responses.GET,
            f"{BASE}/api/v1/mydb/t",
            json={"data": [{"id": 1, "name": "a", "score": 1.5}], "total": 10},
        )
        source = _make_source()
        source.connect()
        schema = source.discover_schema()
        cols = {c.name: c.type for c in schema["t"].columns}
        assert cols == {"id": "integer", "name": "varchar", "score": "double"}
        assert schema["t"].row_count == 10
        assert "limit=5" in responses.calls[2].request.url
