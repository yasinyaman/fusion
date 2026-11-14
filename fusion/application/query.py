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
from fusion.domain.errors import QueryError
from fusion.domain.models import FetchPlan, QueryResult, TableRef
from fusion.domain.query_shape import QueryShape
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
        #: Sources whose SQL has no LIMIT (SQL Server, Oracle): a capped query
        #: failed there and the uncapped one ran, so the cap is not sent again.
        self._no_row_cap: set[str] = set()

    @property
    def planner(self) -> FetchPlanner:
        return self._planner

    @property
    def analyzer(self) -> SqlAnalyzer:
        return self._analyzer

    def sql(
        self,
        query: str,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        auto_load: bool = True,
        params: Sequence[Any] | None = None,
        local_only: bool = False,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Run a read-only query.

        ``params`` bind to ``?`` placeholders and force execution on the
        analytics store (pushdown is skipped because ``?`` binding is
        store-specific).

        ``local_only`` says this SQL was written for the analytics store and
        must not be shipped to a source, whatever the plan would allow. It is a
        correctness flag, not an optimization: the engines disagree on things
        that parse either way and answer differently — interval literals,
        ``NULLS FIRST``/``NULLS LAST`` defaults inside a window, integer
        division. Pushdown swallows failures and falls back, so a *syntax*
        mismatch is merely slow; a *semantic* one is silently wrong.
        """
        self._validator.validate(query)
        # ``max_rows`` stops reading the result there. A capped result is a
        # different answer from the whole one, so it is cached under its own key.
        key = query if max_rows is None else f"{query} /* first {max_rows} rows */"

        if use_cache:
            cached = self._cache.get(key, params)
            if cached is not None:
                logger.debug("Cache hit for query: %s", query[:80])
                return cached

        executed = query
        if auto_load and self._sources.has_sources:
            plan = self._planner.plan_query(query, estimator=self._sources.estimate_slice)

            if (
                params is None
                and not local_only
                and plan.fetch.pushdown_eligible
                and plan.fetch.source_name
            ):
                source = self._sources.pushdown_source(plan.fetch.source_name)
                if source is not None:
                    try:
                        result = self._execute_pushdown(query, plan.fetch, source, max_rows)
                    except Exception as e:
                        logger.info("Pushdown failed, falling back to local execution: %s", e)
                    else:
                        if max_rows is not None:
                            result.rows = result.rows[:max_rows]
                        if use_cache:
                            self._cache.put(key, result, ttl=cache_ttl)
                        return result

            if plan.is_refused:
                raise QueryError(plan.refusal)
            for table_name in plan.evictions:
                self._sources.evict_slice(table_name)
            loaded = self._sources.ensure_slices(plan.targets)
            mapping = {ref: name for ref, name in loaded.items() if name != ref.full_name}
            # ``FROM users`` resolved to ``db.users``, which is the name the
            # store has: point the bare name at it too.
            bare = {r.table for r in self._analyzer.table_references(query) if not r.source}
            for ref, name in loaded.items():
                if ref.table in bare:
                    mapping[TableRef("", ref.table)] = name
            if mapping:
                # Only the table names change; the original WHERE and ON
                # clauses stay, so a slice can only narrow what is scanned.
                executed = self._analyzer.rewrite_tables(query, mapping)
                if executed != query:
                    logger.debug("Reading slices: %s", executed[:160])

        return self._execute_and_cache(
            executed,
            key=key,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            params=params,
            max_rows=max_rows,
        )

    def materialize_for(self, shape: QueryShape) -> dict[TableRef, str]:
        """Plan and load what a pre-analyzed query needs.

        The caller derived ``shape`` itself instead of handing over SQL to be
        parsed, which is what lets a windowed query be sliced at all.

        Returns:
            Where each table now lives in the store — its own name, or a slice.

        Raises:
            QueryError: With the planner's refusal, which already names the
                table, its size, the limit it exceeded and the ways forward.
        """
        plan = self._planner.plan_shape(shape, estimator=self._sources.estimate_slice)
        if plan.is_refused:
            raise QueryError(plan.refusal)
        for table_name in plan.evictions:
            self._sources.evict_slice(table_name)
        return self._sources.ensure_slices(plan.targets)

    def run_local(
        self, sql: str, *, use_cache: bool = True, cache_ttl: int | None = None
    ) -> QueryResult:
        """Validate and run SQL this process generated, against tables it already loaded.

        No planning, no table rewriting and no pushdown: the caller has already
        materialized what the query reads and written the store's own table
        names into it. Guardrails still apply — generated SQL is validated
        exactly like SQL from a caller.
        """
        self._validator.validate(sql)
        if use_cache:
            cached = self._cache.get(sql, None)
            if cached is not None:
                logger.debug("Cache hit for generated query: %s", sql[:80])
                return cached
        return self._execute_and_cache(
            sql, key=sql, use_cache=use_cache, cache_ttl=cache_ttl, params=None
        )

    def _execute_and_cache(
        self,
        executed: str,
        *,
        key: str,
        use_cache: bool,
        cache_ttl: int | None,
        params: Sequence[Any] | None,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Run ``executed`` on the store and cache the result under ``key``."""
        started = time.perf_counter()
        rows = self._store.execute(executed, params, max_rows=max_rows)
        elapsed_ms = (time.perf_counter() - started) * 1000
        result = QueryResult.from_rowset(rows, sql=key, execution_time_ms=elapsed_ms)

        if use_cache:
            self._cache.put(key, result, ttl=cache_ttl, params=params)
        logger.debug("Query executed in %.1fms (%d rows)", elapsed_ms, result.row_count)
        return result

    def _execute_pushdown(
        self, query: str, plan: FetchPlan, source: PushdownCapable, max_rows: int | None = None
    ) -> QueryResult:
        assert plan.source_name is not None
        name = plan.source_name
        rewritten = self._analyzer.strip_source_prefix(query, name)
        started = time.perf_counter()
        capped = (
            self._analyzer.with_row_cap(rewritten, max_rows)
            if max_rows is not None and name not in self._no_row_cap
            else rewritten
        )
        if capped != rewritten:
            # Read no more than the caller will keep. The source's dialect is
            # not known, and LIMIT is not SQL Server's or Oracle's: if the
            # capped query fails, the plain one decides who was at fault.
            logger.info("Pushdown query to %s: %s", name, capped[:120])
            try:
                rows = source.execute_query(capped)
            except QueryError:
                # The source answered and refused; an outage is not retried.
                rows = source.execute_query(rewritten)
                logger.info("Source %s refused LIMIT; pushing queries uncapped", name)
                self._no_row_cap.add(name)
        else:
            logger.info("Pushdown query to %s: %s", name, rewritten[:120])
            rows = source.execute_query(rewritten)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return QueryResult.from_rowset(rows, sql=query, execution_time_ms=elapsed_ms)
