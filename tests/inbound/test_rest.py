"""Tests for the FastAPI REST adapter."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fusion import __version__
from fusion.adapters.inbound.rest.app import create_app
from fusion.adapters.inbound.rest.rate_limit import rate_limit_key
from fusion.adapters.inbound.rest.routes import handle_result
from fusion.application.settings import Settings


@pytest.fixture
def client(app_with_data):
    return TestClient(create_app(app_with_data))


def _client_with(app, **overrides):
    return TestClient(create_app(app, replace(app.settings, **overrides)))


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "healthy",
            "version": __version__,
            "environment": "development",
        }

    def test_readiness_ready(self, client):
        resp = client.get("/readiness")
        assert resp.status_code == 200
        assert resp.json()["sources"] == 1

    def test_readiness_without_sources(self, app):
        resp = TestClient(create_app(app)).get("/readiness")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_request_id_echoed_and_sanitized(self, client):
        resp = client.get("/health", headers={"X-Request-ID": "abc-123\r\nX: y"})
        assert resp.headers["X-Request-ID"] == "abc-123Xy"
        assert client.get("/health").headers["X-Request-ID"]


class TestTools:
    def test_list_tools(self, client):
        data = client.get("/tools").json()
        assert data["count"] == 12
        assert {t["name"] for t in data["tools"]} >= {"query_data", "list_sources", "cache_stats"}

    def test_dispatch(self, client):
        assert "sources" in client.post("/tools/list_sources", json={}).json()
        resp = client.post("/tools/describe_table", json={"table": "test_db.users"})
        assert resp.json()["table"] == "test_db.users"
        assert client.post("/tools/list_sources").status_code == 200  # no body

    def test_dispatch_errors(self, client):
        assert client.post("/tools/nonexistent_tool", json={}).status_code == 404
        assert client.post("/tools/query_data", json={"sql": "DROP TABLE x"}).status_code == 403
        assert client.post("/tools/describe_table", json={}).status_code == 400
        resp = client.post("/tools/query_data", json={"sql": "SELECT 1; SELECT 2"})
        assert resp.status_code == 403

    def test_handle_result_passthrough(self):
        assert handle_result({"rows": []}) == {"rows": []}


class TestConvenienceRoutes:
    def test_sources_and_schema(self, client):
        assert client.get("/sources").json()["sources"][0]["source"] == "test_db"
        data = client.get("/tables/test_db.users/schema").json()
        assert data["table"] == "test_db.users"
        assert len(data["columns"]) == 3
        assert client.get("/tables/test_db.nope/schema").status_code == 400

    def test_query(self, client):
        resp = client.post("/query", json={"sql": "SELECT COUNT(*) AS cnt FROM test_db.users"})
        assert resp.status_code == 200
        assert resp.json()["rows"][0]["cnt"] == 5

    def test_query_guardrail_violation(self, client):
        resp = client.post("/query", json={"sql": "DROP TABLE test_db.users"})
        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_query_union(self, client):
        resp = client.post(
            "/query",
            json={"sql": "SELECT 1 AS x UNION SELECT 2 AS x ORDER BY x"},
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 2

    def test_query_bad_sql_is_400(self, client):
        assert client.post("/query", json={"sql": "SELECT * FROM nope"}).status_code == 400

    def test_search_and_aggregate(self, client):
        resp = client.post(
            "/search",
            json={"table": "test_db.users", "filter_column": "name", "filter_value": "Bob"},
        )
        assert resp.json()["row_count"] == 1
        resp = client.post(
            "/aggregate",
            json={
                "table": "test_db.orders",
                "group_by": "product",
                "agg_column": "amount",
                "agg_func": "SUM",
            },
        )
        assert resp.json()["rows"][0]["product"] == "A"

    def test_views(self, client):
        assert client.get("/views").json() == {"views": []}
        resp = client.post(
            "/views", json={"name": "v", "sql": "SELECT COUNT(*) AS cnt FROM test_db.users"}
        )
        assert resp.json()["status"] == "created"
        assert client.post("/views/v/refresh").json()["status"] == "refreshed"
        assert client.post("/views/nope/refresh").status_code == 400
        assert len(client.get("/views").json()["views"]) == 1

    def test_load_table_and_cache(self, app_lazy):
        c = TestClient(create_app(app_lazy))
        assert c.post("/tables/warp_main.users/load").json()["status"] == "loaded"
        assert c.post("/tables/warp_main.users/load").json()["status"] == "already_loaded"
        assert isinstance(c.get("/cache/stats").json(), dict)


class TestBackupRoutes:
    def test_disabled_backup_is_409(self, client):
        assert client.post("/backup/create").status_code == 409
        assert client.get("/backup/list").json() == {"backups": [], "count": 0}
        assert client.get("/backup/stats").json()["enabled"] is False

    def test_enabled_backup_flow(self, tmp_path, scheduler):
        from fusion.bootstrap import build_app

        settings = Settings(
            threads=1,
            memory_limit="256MB",
            external_access=True,
            backup_enabled=True,
            backup_path=str(tmp_path / "b"),
        )
        app = build_app(settings, scheduler=scheduler)
        try:
            with TestClient(create_app(app)) as c:  # lifespan starts the scheduler
                assert app.backup.running
                resp = c.post("/backup/create")
                assert resp.status_code == 200
                assert resp.json()["backup"]["kind"] == "export"
                assert c.get("/backup/list").json()["count"] == 1
            assert not app.backup.running
        finally:
            app.close()


class TestDebugEndpoint:
    def test_available_outside_production(self, client):
        resp = client.get("/debug/config")
        assert resp.status_code == 200
        assert resp.json()["environment"] == "development"
        assert resp.json()["auth_required"] is False

    def test_hidden_in_production(self, app_with_data):
        c = _client_with(app_with_data, env="production", api_key="k")
        assert c.get("/debug/config", headers={"X-API-Key": "k"}).status_code == 404

    def test_force_enabled_in_production(self, app_with_data):
        c = _client_with(app_with_data, env="production", api_key="k", debug_endpoints="true")
        assert c.get("/debug/config", headers={"X-API-Key": "k"}).status_code == 200


class TestAuth:
    def test_open_when_key_unset(self, client):
        assert client.get("/sources").status_code == 200

    def test_missing_key_rejected(self, app_with_data):
        assert _client_with(app_with_data, api_key="topsecret").get("/sources").status_code == 401

    def test_wrong_key_rejected(self, app_with_data):
        c = _client_with(app_with_data, api_key="topsecret")
        assert c.get("/sources", headers={"X-API-Key": "nope"}).status_code == 403
        assert c.get("/sources", headers={"X-API-Key": "topsecre"}).status_code == 403

    def test_correct_key_allowed(self, app_with_data):
        c = _client_with(app_with_data, api_key="topsecret")
        assert c.get("/sources", headers={"X-API-Key": "topsecret"}).status_code == 200

    def test_health_and_docs_excluded(self, app_with_data):
        c = _client_with(app_with_data, api_key="topsecret")
        assert c.get("/health").status_code == 200
        assert c.get("/readiness").status_code == 200
        assert c.get("/openapi.json").status_code == 200


class TestRateLimit:
    def _req(self, api_key=None, host="9.9.9.9"):
        req = MagicMock()
        req.headers = {"X-API-Key": api_key} if api_key else {}
        req.client.host = host
        return req

    def test_uses_api_key_when_present(self):
        key = rate_limit_key(self._req(api_key="secret-abc"))
        assert key.startswith("key:")
        assert rate_limit_key(self._req(api_key="secret-abc")) == key
        assert "secret-abc" not in key

    def test_distinguishes_api_keys(self):
        assert rate_limit_key(self._req(api_key="aaa")) != rate_limit_key(self._req(api_key="bbb"))

    def test_falls_back_to_client_ip(self):
        assert rate_limit_key(self._req(host="1.2.3.4")) == "1.2.3.4"

    def test_default_limit_enforced(self, app_with_data):
        c = _client_with(app_with_data, rate_limit="3/minute")
        statuses = [c.get("/cache/stats").status_code for _ in range(4)]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429


class TestLoadTableBody:
    def test_load_without_a_body_loads_the_whole_table(self, client):
        response = client.post("/tables/test_db.users/load")
        assert response.status_code == 200
        assert response.json()["status"] in ("loaded", "already_loaded")

    def test_load_with_a_where_body_loads_a_slice(self, client):
        response = client.post(
            "/tables/test_db.orders/load",
            json={"where": "product = 'A'", "columns": ["id", "product"]},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "loaded"
        assert body["slice"] == "columns=id,product where product = 'A'"
        assert body["row_count"] == 3

    def test_an_unpushable_where_is_a_client_error(self, client):
        response = client.post(
            "/tables/test_db.orders/load", json={"where": "product = 'A' OR id = 1"}
        )
        assert response.status_code == 400
        assert "AND of simple conditions" in response.json()["detail"]
