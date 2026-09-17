"""SourceService: connect sources, lazy-load tables, refresh."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.models import TableRef, coerce_ref
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.data_source import DataSource, PushdownCapable, SourceFactory
from fusion.ports.scheduler import ScheduledJob, Scheduler

logger = logging.getLogger(__name__)


class SourceService:
    """Owns the connected DataSources and the loaded-table lifecycle."""

    def __init__(
        self,
        catalog: SchemaCatalog,
        store: AnalyticsStore,
        source_factory: SourceFactory,
        max_ingest_rows: int = 0,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._factory = source_factory
        self._max_ingest_rows = max_ingest_rows
        self._scheduler = scheduler
        self._sources: dict[str, DataSource] = {}
        self._auto_refresh: ScheduledJob | None = None

    # -- connections --------------------------------------------------------

    def connect(self, name: str, config: Mapping[str, Any], fetch_all: bool = False) -> None:
        """Connect a source and register its metadata (data loads lazily).

        ``name`` becomes the schema prefix for the source's tables, e.g.
        ``"mydb"`` -> ``mydb.orders``.
        """
        if not IDENTIFIER_RE.match(name) or "." in name:
            raise ConnectionError(f"Invalid source name '{name}': use letters, digits, underscore")
        if not config.get("type"):
            raise ConnectionError("Config must include 'type' key")

        source = self._factory(name, config)
        source.connect()

        self._store.create_schema(name)
        schema = source.discover_schema()
        self._catalog.register_source(name, source.source_type, schema)
        self._sources[name] = source
        logger.info("Connected source: %s (%d tables, metadata only)", name, len(schema))

        if fetch_all:
            self.ensure_loaded(TableRef(name, table) for table in schema)

    def disconnect(self, name: str) -> None:
        source = self._sources.pop(name, None)
        if source is not None:
            source.close()
        self._store.drop_schema(name)
        self._catalog.unregister_source(name)
        logger.info("Disconnected source: %s", name)

    def source(self, name: str) -> DataSource | None:
        return self._sources.get(name)

    def source_names(self) -> list[str]:
        return list(self._sources)

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    def pushdown_source(self, name: str) -> PushdownCapable | None:
        """The source, if it can execute SQL on its own backend."""
        source = self._sources.get(name)
        if source is None or not source.supports_pushdown:
            return None
        if not isinstance(source, PushdownCapable):
            return None
        return source

    # -- loading ------------------------------------------------------------

    def ensure_loaded(self, refs: Iterable[TableRef | str]) -> list[TableRef]:
        """Materialize any of ``refs`` not loaded yet; returns those newly loaded."""
        newly: list[TableRef] = []
        for raw in refs:
            ref = coerce_ref(raw)
            if self._catalog.is_loaded(ref):
                continue
            source = self._sources.get(ref.source)
            if source is None:
                logger.warning("No connected source '%s'; cannot load %s", ref.source, ref)
                continue
            self._load(ref, source)
            newly.append(ref)
        return newly

    def _load(self, ref: TableRef, source: DataSource) -> None:
        logger.info("On-demand loading: %s", ref)
        rows = source.fetch_table(ref.table, max_rows=self._max_ingest_rows or None)
        schema = self._catalog.get_table(ref) if self._catalog.has_table(ref) else None
        count = self._store.materialize(ref, rows, schema)
        if schema is not None:
            self._catalog.set_row_count(ref, count)
        self._catalog.mark_loaded(ref)
        logger.info("On-demand load complete: %s (%d rows)", ref, count)

    def table_stats(self) -> dict[str, int]:
        """Row counts for every catalog table (-1 when not loaded)."""
        stats: dict[str, int] = {}
        for ref in self._catalog.list_tables():
            if not self._catalog.is_loaded(ref):
                stats[ref.full_name] = -1
                continue
            try:
                stats[ref.full_name] = self._store.count(ref.full_name)
            except QueryError:
                stats[ref.full_name] = -1
        return stats

    # -- refresh ------------------------------------------------------------

    def refresh_all(self, force: bool = False) -> None:
        """Re-discover schemas and re-fetch tables that are already loaded."""
        for name, source in list(self._sources.items()):
            try:
                schema = source.discover_schema()
                for table, table_schema in schema.items():
                    ref = TableRef(name, table)
                    if self._catalog.is_loaded(ref):
                        rows = source.fetch_table(table, max_rows=self._max_ingest_rows or None)
                        table_schema.row_count = self._store.materialize(ref, rows, table_schema)
                self._catalog.register_source(name, source.source_type, schema)
                logger.info("Refreshed source: %s", name)
            except Exception as e:
                logger.error("Failed to refresh source %s: %s", name, e)
                if force:
                    raise

    def start_auto_refresh(self, interval: int = 300) -> None:
        if self._scheduler is None:
            raise RuntimeError("SourceService has no scheduler; auto-refresh unavailable")
        self.stop_auto_refresh()
        self._auto_refresh = self._scheduler.every(interval, self.refresh_all, name="auto-refresh")
        logger.info("Auto-refresh started (every %ds)", interval)

    def stop_auto_refresh(self) -> None:
        if self._auto_refresh is not None:
            self._auto_refresh.cancel()
            self._auto_refresh = None
            logger.info("Auto-refresh stopped")

    @property
    def auto_refresh_running(self) -> bool:
        return self._auto_refresh is not None

    def close_all(self) -> None:
        self.stop_auto_refresh()
        for name, source in list(self._sources.items()):
            try:
                source.close()
            except Exception as e:
                logger.warning("Error closing source %s: %s", name, e)
        self._sources.clear()
