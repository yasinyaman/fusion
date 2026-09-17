"""Contract every QueryCache implementation must honour."""

import pytest

from fusion.adapters.outbound.memory_cache import MemoryQueryCache
from fusion.domain.models import QueryResult


@pytest.fixture(params=[lambda: MemoryQueryCache(max_entries=5, default_ttl=60)], ids=["memory"])
def cache(request):
    return request.param()


class TestQueryCacheContract:
    def test_roundtrip_marks_from_cache(self, cache):
        result = QueryResult(["a"], [(1,)], sql="q", execution_time_ms=2.0)
        assert cache.get("q") is None
        cache.put("q", result)
        hit = cache.get("q")
        assert hit is not None
        assert hit.rows == [(1,)]
        assert hit.from_cache is True

    def test_params_are_part_of_the_key(self, cache):
        result = QueryResult(["a"], [(1,)])
        cache.put("q", result, params=[1])
        assert cache.get("q") is None
        assert cache.get("q", params=[1]) is not None
        assert cache.get("q", params=[2]) is None

    def test_invalidate_clear_stats(self, cache):
        cache.put("q", QueryResult(["a"], []))
        assert cache.invalidate("q") is True
        assert cache.invalidate("q") is False
        cache.clear()
        stats = cache.stats()
        assert set(stats) >= {"hit_rate", "hits", "misses", "cached_queries", "max_entries"}
