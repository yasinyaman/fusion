"""In-process LRU + TTL implementation of the QueryCache port."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from fusion.domain.models import QueryResult
from fusion.domain.sql_text import normalize_sql_for_cache


class MemoryQueryCache:
    """Thread-safe LRU cache keyed on normalized SQL plus bound parameters."""

    def __init__(self, max_entries: int = 500, default_ttl: int = 300) -> None:
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._entries: OrderedDict[str, tuple[QueryResult, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, sql: str, params: Sequence[Any] | None = None) -> QueryResult | None:
        key = self._key(sql, params)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            result, expires_at = entry
            if time.time() > expires_at:
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return result.as_cached()

    def put(
        self,
        sql: str,
        result: QueryResult,
        ttl: int | None = None,
        params: Sequence[Any] | None = None,
    ) -> None:
        key = self._key(sql, params)
        expires_at = time.time() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._entries.pop(key, None)
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[key] = (result, expires_at)

    def invalidate(self, sql: str, params: Sequence[Any] | None = None) -> bool:
        key = self._key(sql, params)
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hit_rate": round(self._hits / total, 2) if total else 0.0,
                "hits": self._hits,
                "misses": self._misses,
                "cached_queries": len(self._entries),
                "max_entries": self._max_entries,
            }

    @staticmethod
    def _key(sql: str, params: Sequence[Any] | None) -> str:
        normalized = normalize_sql_for_cache(sql)
        if params:
            normalized += "\x00PARAMS\x00" + repr(list(params))
        return hashlib.sha256(normalized.encode()).hexdigest()
