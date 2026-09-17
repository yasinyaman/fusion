"""QueryService: the SQL execution pipeline.

validate -> cache lookup -> plan -> pushdown (if eligible) | lazy-load ->
execute on the analytics store -> cache.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from fusion.application.planner import FetchPlanner
from fusion.application.sources import SourceService
from fusion.domain.models import FetchPlan, QueryResult
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.data_source import PushdownCapable
from fusion.ports.sql_policy import SqlAnalyzer, SqlValidator

logger = logging.getLogger(__name__)


class QueryService:
    def __init__(
        self,
        validator: SqlValidator,
        analyzer: SqlAnalyzer,
        planner: FetchPlanner,
        cache: QueryCache,
        store: AnalyticsStore,
        sources: SourceService,
    ) -> None:
        self._validator = validator
        self._analyzer = analyzer
        self._planner = planner
        self._cache = cache
        self._store = store
        self._sources = sources

    def sql(
        self,
        query: str,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        auto_load: bool = True,
        params: Sequence[Any] | None = None,
    ) -> QueryResult:
        """Run a read-only query.

        ``params`` bind to ``?`` placeholders and force execution on the
        analytics store (pushdown is skipped because ``?`` binding is
        store-specific).
        """
        self._validator.validate(query)

        if use_cache:
            cached = self._cache.get(query, params)
            if cached is not None:
                logger.debug("Cache hit for query: %s", query[:80])
                return cached

        if auto_load and self._sources.has_sources:
            plan = self._planner.plan_for_sql(query)

            if params is None and plan.pushdown_eligible and plan.source_name:
                source = self._sources.pushdown_source(plan.source_name)
                if source is not None:
                    try:
                        result = self._execute_pushdown(query, plan, source)
                    except Exception as e:
                        logger.info("Pushdown failed, falling back to local execution: %s", e)
                    else:
                        if use_cache:
                            self._cache.put(query, result, ttl=cache_ttl)
                        return result

            if not plan.is_empty():
                self._sources.ensure_loaded(plan.targets)

        started = time.perf_counter()
        rows = self._store.execute(query, params)
        elapsed_ms = (time.perf_counter() - started) * 1000
        result = QueryResult.from_rowset(rows, sql=query, execution_time_ms=elapsed_ms)

        if use_cache:
            self._cache.put(query, result, ttl=cache_ttl, params=params)
        logger.debug("Query executed in %.1fms (%d rows)", elapsed_ms, result.row_count)
        return result

    def _execute_pushdown(
        self, query: str, plan: FetchPlan, source: PushdownCapable
    ) -> QueryResult:
        assert plan.source_name is not None
        rewritten = self._analyzer.strip_source_prefix(query, plan.source_name)
        logger.info("Pushdown query to %s: %s", plan.source_name, rewritten[:120])
        started = time.perf_counter()
        rows = source.execute_query(rewritten)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return QueryResult.from_rowset(rows, sql=query, execution_time_ms=elapsed_ms)
