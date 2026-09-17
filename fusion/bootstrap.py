"""Composition root: wire adapters into the application services.

This is the only module that imports both the application layer and the
concrete adapters. Every dependency can be overridden (tests inject fakes).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fusion.application.app import FusionApp
from fusion.application.backup import BackupService
from fusion.application.planner import FetchPlanner
from fusion.application.query import QueryService
from fusion.application.settings import Settings
from fusion.application.sources import SourceService
from fusion.application.tools import ToolService
from fusion.application.views import MaterializedViewService
from fusion.domain.catalog import SchemaCatalog
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.data_source import DatabaseDiscovery, SourceFactory
from fusion.ports.scheduler import Scheduler
from fusion.ports.sql_policy import SqlAnalyzer, SqlValidator


def build_app(
    settings: Settings | None = None,
    *,
    store: AnalyticsStore | None = None,
    cache: QueryCache | None = None,
    scheduler: Scheduler | None = None,
    source_factory: SourceFactory | None = None,
    validator: SqlValidator | None = None,
    analyzer: SqlAnalyzer | None = None,
    clock: Callable[[], float] = time.time,
) -> FusionApp:
    """Build a ready-to-use FusionApp from ``settings`` (defaults: DuckDB in-memory,
    in-process cache, threading scheduler, Warp source registry)."""
    settings = settings or Settings()

    if store is None:
        from fusion.adapters.outbound.duckdb_store import DuckDBStore

        store = DuckDBStore(
            database=settings.database,
            threads=settings.threads,
            memory_limit=settings.memory_limit,
            external_access=settings.external_access,
            max_temp_directory_size=settings.max_temp_directory_size or None,
        )
    if cache is None:
        from fusion.adapters.outbound.memory_cache import MemoryQueryCache

        cache = MemoryQueryCache(
            max_entries=settings.cache_max_entries, default_ttl=settings.cache_ttl
        )
    if scheduler is None:
        from fusion.adapters.outbound.threading_scheduler import ThreadingScheduler

        scheduler = ThreadingScheduler()
    if validator is None or analyzer is None:
        from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator

        validator = validator or SqlglotValidator()
        analyzer = analyzer or SqlglotAnalyzer()
    if source_factory is None:
        from fusion.adapters.outbound.registry import default_registry

        source_factory = default_registry(settings.warp_http_defaults()).create

    catalog = SchemaCatalog()
    planner = FetchPlanner(catalog, analyzer)
    sources = SourceService(
        catalog,
        store,
        source_factory,
        max_ingest_rows=settings.max_ingest_rows,
        scheduler=scheduler,
    )
    query = QueryService(validator, analyzer, planner, cache, store, sources)
    views = MaterializedViewService(store, validator, planner, sources, scheduler, clock)
    backup = BackupService(
        store,
        settings.backup_path,
        interval=settings.backup_interval,
        retention_days=settings.backup_retention_days,
        enabled=settings.backup_enabled,
        scheduler=scheduler,
        clock=clock,
    )
    tools = ToolService(query, sources, views, store, catalog, cache)

    return FusionApp(
        settings=settings,
        catalog=catalog,
        store=store,
        cache=cache,
        scheduler=scheduler,
        sources=sources,
        query=query,
        views=views,
        backup=backup,
        tools=tools,
    )


def default_discovery(settings: Settings | None = None) -> DatabaseDiscovery:
    """The Warp database-discovery adapter (used by the CLIs for --auto-discover).

    With ``settings`` the Warp API key and header are applied to the probe.
    """
    from fusion.adapters.outbound.warp.discovery import WarpDiscovery

    if settings is None:
        return WarpDiscovery()
    return WarpDiscovery(
        api_key=settings.warp_api_key or None, api_key_header=settings.warp_api_key_header
    )
