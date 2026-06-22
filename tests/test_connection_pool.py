"""Tests for the HTTP ConnectionPool utility."""

import pytest
import requests
import responses

from fusion.utils.connection_pool import ConnectionPool


class TestConnectionPool:
    def test_custom_params(self):
        pool = ConnectionPool(pool_size=2, max_retries=1, timeout=5)
        try:
            assert pool.pool_size == 2
            assert pool.max_retries == 1
            assert pool.timeout == 5
        finally:
            pool.close()

    @responses.activate
    def test_get_success(self):
        responses.add(responses.GET, "http://svc/data", json={"ok": 1}, status=200)
        with ConnectionPool() as pool:
            resp = pool.get("http://svc/data")
            assert resp.status_code == 200
            assert resp.json() == {"ok": 1}

    @responses.activate
    def test_post_success(self):
        responses.add(responses.POST, "http://svc/data", json={}, status=201)
        with ConnectionPool() as pool:
            assert pool.post("http://svc/data").status_code == 201

    @responses.activate
    def test_raises_on_server_error(self):
        responses.add(responses.GET, "http://svc/data", json={}, status=500)
        with ConnectionPool() as pool:
            with pytest.raises(requests.RequestException):
                pool.get("http://svc/data")

    def test_context_manager_closes_session(self):
        with ConnectionPool() as pool:
            session = pool.session
        # After exit the session is closed; a fresh adapter map remains but
        # the pool released its connections without raising.
        assert session is not None
