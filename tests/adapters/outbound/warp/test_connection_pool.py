"""Tests for the pooled requests session."""

import pytest
import requests
import responses

from fusion.adapters.outbound.warp.connection_pool import ConnectionPool


class TestConnectionPool:
    def test_custom_params_and_headers(self):
        with ConnectionPool(pool_size=2, max_retries=1, timeout=5, api_key="k") as pool:
            assert pool.pool_size == 2
            assert pool.max_retries == 1
            assert pool.timeout == 5
            assert pool.session.max_redirects == 0
            assert pool.session.headers["Authorization"] == "Bearer k"
            assert pool.session.headers["Accept"] == "application/json"

    def test_no_auth_header_without_key(self):
        with ConnectionPool() as pool:
            assert "Authorization" not in pool.session.headers

    @responses.activate
    def test_get_and_post_success(self):
        responses.add(responses.GET, "http://svc/data", json={"ok": 1}, status=200)
        responses.add(responses.POST, "http://svc/data", json={}, status=201)
        with ConnectionPool() as pool:
            assert pool.get("http://svc/data").json() == {"ok": 1}
            assert pool.post("http://svc/data").status_code == 201

    @responses.activate
    def test_raises_on_server_error(self):
        responses.add(responses.GET, "http://svc/data", json={}, status=500)
        with ConnectionPool(max_retries=0) as pool:
            with pytest.raises(requests.RequestException):
                pool.get("http://svc/data")

    def test_retry_strategy_mounted(self):
        with ConnectionPool(max_retries=4, backoff_factor=1.5) as pool:
            adapter = pool.session.get_adapter("http://x")
            assert adapter.max_retries.total == 4
            assert adapter.max_retries.backoff_factor == 1.5
            assert 503 in adapter.max_retries.status_forcelist
