"""HTTP-contract tests for WarpConnector using the `responses` library.

Unlike test_warp.py (which mocks the Session object), these exercise the real
requests flow — URL building, query params, status handling, request bodies —
so they catch drift in the Warp REST API contract.
"""

import pytest
import responses

from fusion.connectors.warp import WarpConnector
from fusion.exceptions import ConnectionError, QueryError

BASE = "http://localhost:8080"


def _make_connector(**overrides):
    cfg = {"type": "warp", "base_url": BASE, "database": "mydb"}
    cfg.update(overrides)
    return WarpConnector("warp", cfg)


def _stub_connect(tables=("users", "orders")):
    responses.add(responses.GET, f"{BASE}/health", json={"status": "ok"}, status=200)
    responses.add(
        responses.GET,
        f"{BASE}/info",
        json={"databases": {"mydb": {"tables": list(tables)}}},
        status=200,
    )


class TestWarpConnectorContract:
    @responses.activate
    def test_connect_discovers_tables(self):
        _stub_connect()
        conn = _make_connector()
        conn.connect()
        assert conn.is_connected
        assert set(conn._tables) == {"users", "orders"}

    @responses.activate
    def test_health_500_raises_connection_error(self):
        responses.add(responses.GET, f"{BASE}/health", json={"err": "x"}, status=500)
        conn = _make_connector()
        with pytest.raises(ConnectionError):
            conn.connect()

    @responses.activate
    def test_fetch_data_paginates(self):
        _stub_connect(("t",))
        # page1 is full (page_size=2) -> keep going; page2 is partial -> stop
        responses.add(
            responses.GET, f"{BASE}/api/v1/mydb/t",
            json={"data": [{"id": 1}, {"id": 2}]}, status=200,
        )
        responses.add(
            responses.GET, f"{BASE}/api/v1/mydb/t",
            json={"data": [{"id": 3}]}, status=200,
        )
        conn = _make_connector(page_size=2)
        conn.connect()
        df = conn.fetch_data("t")
        assert list(df["id"]) == [1, 2, 3]

    @responses.activate
    def test_fetch_data_respects_max_rows(self):
        _stub_connect(("t",))
        responses.add(
            responses.GET, f"{BASE}/api/v1/mydb/t",
            json={"data": [{"id": 1}, {"id": 2}]}, status=200,
        )
        conn = _make_connector(page_size=10)
        conn.connect()
        df = conn.fetch_data("t", max_rows=1)
        assert len(df) == 1

    @responses.activate
    def test_execute_query_sends_sql_in_body(self):
        _stub_connect(("t",))
        responses.add(
            responses.POST, f"{BASE}/api/v1/mydb/query/execute",
            json={"data": [{"cnt": 42}]}, status=200,
        )
        conn = _make_connector()
        conn.connect()
        df = conn.execute_query("SELECT COUNT(*) AS cnt FROM t")
        assert df.iloc[0]["cnt"] == 42
        assert b"SELECT" in responses.calls[-1].request.body

    @responses.activate
    def test_execute_query_500_raises_query_error(self):
        _stub_connect(("t",))
        responses.add(
            responses.POST, f"{BASE}/api/v1/mydb/query/execute",
            json={"error": "bad"}, status=500,
        )
        conn = _make_connector()
        conn.connect()
        with pytest.raises(QueryError):
            conn.execute_query("SELECT 1")

    @responses.activate
    def test_bearer_auth_header_sent(self):
        _stub_connect(())
        conn = _make_connector(api_key="s3cr3t")
        conn.connect()
        assert responses.calls[0].request.headers["Authorization"] == "Bearer s3cr3t"

    @responses.activate
    def test_get_schema_infers_types(self):
        _stub_connect(("t",))
        responses.add(
            responses.GET, f"{BASE}/api/v1/mydb/t",
            json={"data": [{"id": 1, "name": "a", "score": 1.5}]}, status=200,
        )
        conn = _make_connector()
        conn.connect()
        schema = conn.get_schema()
        cols = {c["name"]: c["type"] for c in schema["t"]["columns"]}
        assert cols["id"] == "integer"
        assert cols["score"] == "double"
        assert cols["name"] == "varchar"

    @responses.activate
    def test_discover_databases_contract(self):
        responses.add(responses.GET, f"{BASE}/health", json={}, status=200)
        responses.add(
            responses.GET, f"{BASE}/info",
            json={"databases": {"db1": {}, "db2": {}}}, status=200,
        )
        dbs = WarpConnector.discover_databases(BASE)
        assert set(dbs) == {"db1", "db2"}
