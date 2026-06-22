"""Tests for FastAPI REST API server."""

import pytest

from fusion.tools.executor import ToolExecutor


@pytest.fixture
def rest_client(engine_with_data):
    """FastAPI TestClient backed by engine with test data."""
    from fastapi.testclient import TestClient
    from fusion.tools.rest_server import create_app

    executor = ToolExecutor(engine_with_data)
    app = create_app(engine=engine_with_data, executor=executor)
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, rest_client):
        resp = rest_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data


class TestToolsDiscovery:
    def test_list_tools(self, rest_client):
        resp = rest_client.get("/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 10
        assert len(data["tools"]) == 10

    def test_tools_have_names(self, rest_client):
        resp = rest_client.get("/tools")
        names = [t["name"] for t in resp.json()["tools"]]
        assert "query_data" in names
        assert "list_sources" in names
        assert "cache_stats" in names


class TestSourcesEndpoint:
    def test_get_sources(self, rest_client):
        resp = rest_client.get("/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert len(data["sources"]) == 1
        assert data["sources"][0]["source"] == "test_db"

    def test_get_table_schema(self, rest_client):
        resp = rest_client.get("/tables/test_db.users/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert data["table"] == "test_db.users"
        assert len(data["columns"]) == 3


class TestQueryEndpoint:
    def test_query(self, rest_client):
        resp = rest_client.post("/query", json={"sql": "SELECT COUNT(*) as cnt FROM test_db.users"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"][0]["cnt"] == 5

    def test_query_guardrail_violation(self, rest_client):
        resp = rest_client.post("/query", json={"sql": "DROP TABLE test_db.users"})
        assert resp.status_code == 403


class TestToolDispatch:
    def test_dispatch_list_sources(self, rest_client):
        resp = rest_client.post("/tools/list_sources", json={})
        assert resp.status_code == 200
        assert "sources" in resp.json()

    def test_dispatch_unknown_tool(self, rest_client):
        resp = rest_client.post("/tools/nonexistent_tool", json={})
        assert resp.status_code == 404

    def test_dispatch_describe_table(self, rest_client):
        resp = rest_client.post(
            "/tools/describe_table",
            json={"table": "test_db.users"},
        )
        assert resp.status_code == 200
        assert resp.json()["table"] == "test_db.users"


class TestCacheEndpoint:
    def test_cache_stats(self, rest_client):
        resp = rest_client.get("/cache/stats")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)


class TestDebugEndpoint:
    def test_debug_config_available_outside_production(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "ENV", "development")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "")
        resp = rest_client.get("/debug/config")
        assert resp.status_code == 200
        assert "environment" in resp.json()

    def test_debug_config_hidden_in_production(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "")
        resp = rest_client.get("/debug/config")
        assert resp.status_code == 404

    def test_debug_config_force_enabled_in_production(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "true")
        resp = rest_client.get("/debug/config")
        assert resp.status_code == 200


class TestAuthMiddleware:
    """API-key auth is enforced when FUSION_API_KEY is set."""

    def test_open_when_key_unset(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "API_KEY", "")
        assert rest_client.get("/sources").status_code == 200

    def test_missing_key_rejected(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "API_KEY", "topsecret")
        assert rest_client.get("/sources").status_code == 401

    def test_wrong_key_rejected(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "API_KEY", "topsecret")
        resp = rest_client.get("/sources", headers={"X-API-Key": "nope"})
        assert resp.status_code == 403

    def test_correct_key_allowed(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "API_KEY", "topsecret")
        resp = rest_client.get("/sources", headers={"X-API-Key": "topsecret"})
        assert resp.status_code == 200

    def test_health_excluded_from_auth(self, rest_client, monkeypatch):
        from fusion.config import Config
        monkeypatch.setattr(Config, "API_KEY", "topsecret")
        assert rest_client.get("/health").status_code == 200


class TestRateLimitKey:
    """The rate-limit bucket key is per-API-key (anti-spoof / anti-NAT)."""

    def _req(self, api_key=None, host="9.9.9.9"):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = {"X-API-Key": api_key} if api_key else {}
        req.client.host = host
        return req

    def test_uses_api_key_when_present(self):
        from fusion.tools.rest_server import _rate_limit_key

        key = _rate_limit_key(self._req(api_key="secret-abc"))
        assert key.startswith("key:")
        # Stable for the same key, and doesn't leak the raw secret
        assert _rate_limit_key(self._req(api_key="secret-abc")) == key
        assert "secret-abc" not in key

    def test_distinguishes_api_keys(self):
        from fusion.tools.rest_server import _rate_limit_key

        assert _rate_limit_key(self._req(api_key="aaa")) != _rate_limit_key(
            self._req(api_key="bbb")
        )

    def test_falls_back_to_client_ip(self):
        from fusion.tools.rest_server import _rate_limit_key

        assert _rate_limit_key(self._req(host="1.2.3.4")) == "1.2.3.4"


class TestViewsEndpoint:
    def test_list_views(self, rest_client):
        resp = rest_client.get("/views")
        assert resp.status_code == 200
        assert "views" in resp.json()

    def test_create_view(self, rest_client):
        resp = rest_client.post("/views", json={
            "name": "test_view",
            "sql": "SELECT COUNT(*) as cnt FROM test_db.users",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
