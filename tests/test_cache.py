"""Tests for query cache."""

import time

from fusion.cache import QueryCache


class TestQueryCache:
    def test_put_and_get(self, cache):
        cache.put("SELECT 1", "result_1")
        assert cache.get("SELECT 1") == "result_1"

    def test_cache_miss(self, cache):
        assert cache.get("SELECT nonexistent") is None

    def test_ttl_expiration(self):
        c = QueryCache(max_entries=10, default_ttl=1)
        c.put("SELECT 1", "result")
        assert c.get("SELECT 1") == "result"
        time.sleep(1.1)
        assert c.get("SELECT 1") is None

    def test_lru_eviction(self):
        c = QueryCache(max_entries=3, default_ttl=300)
        c.put("q1", "r1")
        c.put("q2", "r2")
        c.put("q3", "r3")
        c.put("q4", "r4")  # Should evict q1
        assert c.get("q1") is None
        assert c.get("q2") == "r2"

    def test_invalidate(self, cache):
        cache.put("SELECT 1", "result")
        assert cache.invalidate("SELECT 1") is True
        assert cache.get("SELECT 1") is None

    def test_invalidate_nonexistent(self, cache):
        assert cache.invalidate("SELECT nothing") is False

    def test_clear(self, cache):
        cache.put("q1", "r1")
        cache.put("q2", "r2")
        cache.clear()
        assert cache.get("q1") is None
        assert cache.get("q2") is None

    def test_stats(self, cache):
        cache.put("q1", "r1")
        cache.get("q1")  # hit
        cache.get("q2")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["cached_queries"] == 1
        assert stats["hit_rate"] == 0.5

    def test_stats_empty(self, cache):
        stats = cache.stats()
        assert stats["hit_rate"] == 0.0
        assert stats["cached_queries"] == 0

    def test_case_insensitive_normalization(self, cache):
        cache.put("SELECT * FROM users", "result")
        # Same query with different case should hit cache
        assert cache.get("select * from users") == "result"

    def test_whitespace_normalization(self, cache):
        cache.put("SELECT  *  FROM  users", "result")
        assert cache.get("SELECT * FROM users") == "result"
