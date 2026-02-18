"""LRU query cache with TTL support."""

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class QueryCache:
    """Thread-safe LRU cache for query results with TTL support."""

    def __init__(self, max_entries: int = 500, default_ttl: int = 300):
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, sql: str) -> Optional[Any]:
        """Get cached result for a query. Returns None on cache miss."""
        key = self._hash_query(sql)
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            entry = self._cache[key]

            # Check TTL
            if time.time() > entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return entry["result"]

    def put(self, sql: str, result: Any, ttl: Optional[int] = None) -> None:
        """Store a query result in cache."""
        key = self._hash_query(sql)
        ttl = ttl if ttl is not None else self._default_ttl

        with self._lock:
            # Remove if exists (to update position)
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if at capacity
            while len(self._cache) >= self._max_entries:
                self._cache.popitem(last=False)

            self._cache[key] = {
                "result": result,
                "expires_at": time.time() + ttl,
                "created_at": time.time(),
            }

    def invalidate(self, sql: str) -> bool:
        """Remove a specific query from cache. Returns True if found."""
        key = self._hash_query(sql)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all cached entries and reset stats."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hit_rate": round(self._hits / total, 2) if total > 0 else 0.0,
                "hits": self._hits,
                "misses": self._misses,
                "cached_queries": len(self._cache),
                "max_entries": self._max_entries,
            }

    @staticmethod
    def _hash_query(sql: str) -> str:
        """Normalize and hash a SQL query for cache key."""
        # Normalize whitespace and case for consistent caching
        normalized = " ".join(sql.split()).strip().upper()
        return hashlib.sha256(normalized.encode()).hexdigest()
