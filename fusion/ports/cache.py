"""Outbound port: query result cache."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from fusion.domain.models import QueryResult


class QueryCache(Protocol):
    def get(self, sql: str, params: Sequence[Any] | None = None) -> QueryResult | None:
        """Cached result (already flagged ``from_cache``), or None."""
        ...

    def put(
        self,
        sql: str,
        result: QueryResult,
        ttl: int | None = None,
        params: Sequence[Any] | None = None,
    ) -> None: ...

    def invalidate(self, sql: str, params: Sequence[Any] | None = None) -> bool: ...

    def clear(self) -> None: ...

    def stats(self) -> dict[str, Any]:
        """``hit_rate, hits, misses, cached_queries, max_entries``."""
        ...
