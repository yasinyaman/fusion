"""Tests for the in-memory query cache."""

import time

import pytest

from fusion.adapters.outbound.memory_cache import MemoryQueryCache
from fusion.domain.models import QueryResult


def _result(tag: str) -> QueryResult:
    return QueryResult(columns=["v"], rows=[(tag,)], sql=tag, execution_time_ms=3.0)


@pytest.fixture
def cache():
    return MemoryQueryCache(max_entries=10, default_ttl=60)


class TestMemoryQueryCache:
    def test_put_and_get_marks_from_cache(self, cache):
        cache.put("SELECT 1", _result("r1"))
        hit = cache.get("SELECT 1")
        assert hit is not None
        assert hit.rows == [("r1",)]
        assert hit.from_cache is True
        assert hit.execution_time_ms == 0.0

    def test_miss(self, cache):
        assert cache.get("SELECT nope") is None

    def test_ttl_expiration(self):
        c = MemoryQueryCache(max_entries=10, default_ttl=1)
        c.put("SELECT 1", _result("r"))
        assert c.get("SELECT 1") is not None
        time.sleep(1.1)
        assert c.get("SELECT 1") is None

    def test_per_entry_ttl(self, cache):
        cache.put("q", _result("r"), ttl=0)
        time.sleep(0.01)
        assert cache.get("q") is None

    def test_lru_eviction(self):
        c = MemoryQueryCache(max_entries=3, default_ttl=300)
        for i in range(1, 5):
            c.put(f"q{i}", _result(f"r{i}"))
        assert c.get("q1") is None
        assert c.get("q2") is not None

    def test_invalidate_and_clear(self, cache):
        cache.put("q1", _result("r1"))
        assert cache.invalidate("q1") is True
        assert cache.invalidate("q1") is False
        cache.put("q2", _result("r2"))
        cache.clear()
        assert cache.get("q2") is None
        assert cache.stats()["hits"] == 0

    def test_stats(self, cache):
        cache.put("q1", _result("r1"))
        cache.get("q1")
        cache.get("q2")
        stats = cache.stats()
        assert stats == {
            "hit_rate": 0.5,
            "hits": 1,
            "misses": 1,
            "cached_queries": 1,
            "max_entries": 10,
        }

    def test_case_and_whitespace_normalization(self, cache):
        cache.put("SELECT  *  FROM  users", _result("r"))
        assert cache.get("select * from users") is not None

    def test_literal_case_is_significant(self, cache):
        """Regression: 'alice' and 'ALICE' must not share a cache entry."""
        cache.put("SELECT * FROM t WHERE name = 'alice'", _result("lower"))
        cache.put("SELECT * FROM t WHERE name = 'ALICE'", _result("upper"))
        assert cache.get("SELECT * FROM t WHERE name = 'alice'").rows == [("lower",)]
        assert cache.get("SELECT * FROM t WHERE name = 'ALICE'").rows == [("upper",)]

    def test_params_produce_distinct_keys(self, cache):
        sql = "SELECT * FROM users WHERE name = ?"
        cache.put(sql, _result("alice"), params=["alice"])
        cache.put(sql, _result("ALICE"), params=["ALICE"])
        assert cache.get(sql, params=["alice"]).rows == [("alice",)]
        assert cache.get(sql, params=["ALICE"]).rows == [("ALICE",)]
        assert cache.get(sql, params=["bob"]) is None
        assert cache.get(sql) is None
        assert cache.invalidate(sql, params=["alice"]) is True
