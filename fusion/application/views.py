"""MaterializedViewService: ``mv_*`` tables with scheduled refresh."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fusion.application.planner import FetchPlanner
from fusion.application.sources import SourceService
from fusion.domain.errors import QueryError
from fusion.domain.identifiers import is_valid_view_name
from fusion.domain.models import QueryResult
from fusion.domain.views import ViewSpec, parse_refresh_interval
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.scheduler import ScheduledJob, Scheduler
from fusion.ports.sql_policy import SqlValidator

logger = logging.getLogger(__name__)


class MaterializedViewService:
    def __init__(
        self,
        store: AnalyticsStore,
        validator: SqlValidator,
        planner: FetchPlanner,
        sources: SourceService,
        scheduler: Scheduler | None = None,
        clock: Callable[[], float] = time.time,
        on_data_changed: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._validator = validator
        self._planner = planner
        self._sources = sources
        self._scheduler = scheduler
        self._clock = clock
        self._on_data_changed = on_data_changed
        self._views: dict[str, ViewSpec] = {}
        self._jobs: dict[str, ScheduledJob] = {}

    def create(
        self, name: str, sql: str, refresh: str = "manual", priority: str = "normal"
    ) -> ViewSpec:
        if not is_valid_view_name(name):
            raise QueryError(f"Invalid view name: '{name}'. Use alphanumeric and underscore only.")
        self._validator.validate(sql)

        now = self._clock()
        spec = ViewSpec(
            name=name, sql=sql, refresh=refresh, priority=priority, created_at=now, last_refresh=now
        )
        try:
            self._materialize(spec)
        except QueryError as e:
            raise QueryError(f"Failed to create materialized view '{name}': {e}") from e
        self._views[name] = spec

        interval = parse_refresh_interval(refresh)
        if interval > 0:
            self._schedule(name, interval)
        elif refresh.lower().strip() != "manual":
            logger.warning("Unknown refresh interval '%s' for view '%s'; manual", refresh, name)
        logger.info("Created materialized view '%s' (refresh=%s)", name, refresh)
        return spec

    def _materialize(self, spec: ViewSpec) -> None:
        # Load any source tables the view reads before creating it, so a view
        # can be defined before its tables were ever queried.
        plan = self._planner.plan_for_sql(spec.sql)
        if not plan.is_empty():
            self._sources.ensure_loaded(plan.targets)
        self._store.create_table_as(spec.table_name, spec.sql)
        # The view's table is new or different now, so any cached result that
        # read it is stale.
        if self._on_data_changed is not None:
            self._on_data_changed()

    def refresh(self, name: str, force: bool = False) -> None:
        spec = self.get(name)
        try:
            self._materialize(spec)
        except QueryError as e:
            raise QueryError(f"Failed to refresh view '{name}': {e}") from e
        spec.last_refresh = self._clock()
        logger.info("Refreshed materialized view '%s'", name)

    def refresh_all(self, force: bool = False) -> None:
        for spec in sorted(self._views.values(), key=lambda s: s.priority_rank):
            try:
                self.refresh(spec.name, force=force)
            except Exception as e:
                logger.error("Failed to refresh view '%s': %s", spec.name, e)
                if force:
                    raise

    def drop(self, name: str) -> None:
        spec = self.get(name)
        job = self._jobs.pop(name, None)
        if job is not None:
            job.cancel()
        self._store.drop_table(spec.table_name)
        del self._views[name]
        logger.info("Dropped materialized view '%s'", name)

    def get(self, name: str) -> ViewSpec:
        try:
            return self._views[name]
        except KeyError:
            raise QueryError(f"Materialized view '{name}' not found") from None

    def has(self, name: str) -> bool:
        return name in self._views

    def rows(self, name: str) -> QueryResult:
        spec = self.get(name)
        started = time.perf_counter()
        rowset = self._store.execute(f'SELECT * FROM "{spec.table_name}"')
        elapsed = (time.perf_counter() - started) * 1000
        return QueryResult.from_rowset(
            rowset, sql=f"SELECT * FROM {spec.table_name}", execution_time_ms=elapsed
        )

    def describe(self, table_name: str) -> dict[str, Any]:
        """Columns and row count of an ``mv_*`` table (introspected from the store)."""
        columns = self._store.describe(table_name)
        if not columns:
            raise QueryError(f"Materialized view '{table_name}' not found")
        return {
            "table": table_name,
            "columns": [c.as_dict() for c in columns],
            "row_count": self._store.count(table_name),
        }

    def list_views(self) -> list[dict[str, Any]]:
        return [spec.as_dict() for spec in self._views.values()]

    def _schedule(self, name: str, interval: int) -> None:
        if self._scheduler is None:
            logger.warning("No scheduler; view '%s' will not auto-refresh", name)
            return

        def refresh_job() -> None:
            if name in self._views:
                self.refresh(name)

        self._jobs[name] = self._scheduler.every(interval, refresh_job, name=f"mv:{name}")

    def close(self) -> None:
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()
