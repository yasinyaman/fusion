"""SourceService: connect sources, lazy-load tables, refresh."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from fusion.application.semijoin import SemiJoinExecutor
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.models import RefreshSpec, TableRef, TableSchema, coerce_ref
from fusion.domain.policy import MaterializationPolicy, TargetPlan
from fusion.domain.slices import LoadedSlice, Predicate, SliceSpec
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.data_source import DataSource, PushdownCapable, SourceFactory
from fusion.ports.scheduler import ScheduledJob, Scheduler

logger = logging.getLogger(__name__)

STAGING_SUFFIX = "__tmp"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class SourceService:
    """Owns the connected DataSources and the loaded-table lifecycle."""

    def __init__(
        self,
        catalog: SchemaCatalog,
        store: AnalyticsStore,
        source_factory: SourceFactory,
        max_ingest_rows: int = 0,
        scheduler: Scheduler | None = None,
        policy: MaterializationPolicy | None = None,
        clock: Callable[[], float] = time.time,
        refresh_config: Mapping[str, RefreshSpec] | None = None,
        on_data_changed: Callable[[], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._factory = source_factory
        self._max_ingest_rows = max_ingest_rows
        self._scheduler = scheduler
        self._policy = policy or MaterializationPolicy()
        self._clock = clock
        self._semi_join_executor = SemiJoinExecutor(
            store, catalog, self._policy, clock, max_ingest_rows
        )
        self._refresh_specs: dict[str, RefreshSpec] = dict(refresh_config or {})
        self._on_data_changed = on_data_changed
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
        self._register_refresh_specs(name, config)

        self._store.create_schema(name)
        schema = source.discover_schema()
        self._catalog.register_source(name, source.source_type, schema)
        self._sources[name] = source
        logger.info("Connected source: %s (%d tables, metadata only)", name, len(schema))

        if fetch_all:
            self.ensure_loaded(TableRef(name, table) for table in schema)

    def _register_refresh_specs(self, name: str, config: Mapping[str, Any]) -> None:
        """Pick up per-table refresh hints from a source's own config.

        ``{"refresh": {"orders": {"watermark_column": "updated_at",
        "key_columns": ["id"]}}}``. Settings-level entries win, since they
        are what an operator set for this deployment.
        """
        from fusion.application.settings import refresh_spec_from

        tables = config.get("refresh")
        if not isinstance(tables, Mapping):
            return
        for table, raw in tables.items():
            spec = refresh_spec_from(raw)
            full_name = f"{name}.{table}"
            if spec is not None and spec.is_incremental and full_name not in self._refresh_specs:
                self._refresh_specs[full_name] = spec

    def refresh_spec(self, ref: TableRef | str) -> RefreshSpec | None:
        """How this table is refreshed, when it was configured for it."""
        return self._refresh_specs.get(coerce_ref(ref).full_name)

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
        loaded = self._materialize(ref, SliceSpec.FULL, source)
        logger.info("On-demand load complete: %s (%d rows)", ref, loaded.row_count)

    # -- slices -------------------------------------------------------------

    def ensure_slices(self, targets: Sequence[TargetPlan]) -> dict[TableRef, str]:
        """Materialize what each target needs; returns ref -> table to query.

        Targets arrive in dependency order (a semi-join's driver first), so
        this walks them as given.
        """
        mapping: dict[TableRef, str] = {}
        for target in targets:
            if target.action == "refuse":
                raise QueryError(target.reason)
            source = self._sources.get(target.ref.source)
            if source is None:
                logger.warning(
                    "No connected source '%s'; cannot load %s", target.ref.source, target.ref
                )
                continue
            mapping[target.ref] = self._apply(target, source)
        return mapping

    def _apply(self, target: TargetPlan, source: DataSource) -> str:
        if target.action == "reuse" and target.covering is not None:
            self._catalog.touch_slice(target.covering.table_name, self._clock())
            return target.covering.table_name
        if target.action == "semi_join" and target.semi_join is not None:
            return self._semi_join(target, source)
        spec = SliceSpec.FULL if target.action == "load_full" else target.spec
        return self._materialize(target.ref, spec, source).table_name

    def _semi_join(self, target: TargetPlan, source: DataSource) -> str:
        """Fetch only the rows whose key appears in the already-loaded driver."""
        return self._semi_join_executor.execute(target, source).table_name

    def _materialize(
        self,
        ref: TableRef,
        spec: SliceSpec,
        source: DataSource,
        derived_from: str | None = None,
    ) -> LoadedSlice:
        """Stream a slice into a staging table, then publish it with a rename.

        The rename is what makes a reload atomic: queries either see the old
        table or the new one, never a half-written one.
        """
        table_name = spec.table_name_for(ref)
        staging = f"{table_name}{STAGING_SUFFIX}"
        max_rows = self._max_ingest_rows or None
        stream = source.fetch_slice(ref.table, spec, max_rows=max_rows)
        try:
            count = self._store.materialize_stream(
                staging, stream, stream.schema or self._slice_schema(ref, spec)
            )
        except Exception:
            self._store.drop_table(staging)
            raise
        self._store.rename_table(staging, table_name)

        # A slice the *source* cut short (Warp's export.max_rows) is not proof
        # of what the table holds, so it must not answer a narrower query.
        # Our own max_ingest_rows is a deliberate ceiling: within this engine
        # the truncated table is the table.
        applied: int | None = getattr(stream, "row_limit", max_rows)
        source_capped = applied is not None and (max_rows is None or applied < max_rows)
        complete = not (source_capped and applied is not None and count >= applied)
        now = self._clock()
        loaded = self._catalog.record_slice(
            LoadedSlice(
                ref=ref,
                spec=spec,
                table_name=table_name,
                row_count=count,
                loaded_at=now,
                last_used=now,
                complete=complete,
                derived_from=derived_from,
            )
        )
        if spec.is_full and self._catalog.has_table(ref):
            self._catalog.set_row_count(ref, count)
        logger.info(
            "Loaded %s into %s (%d rows, %s)",
            spec.describe(),
            table_name,
            count,
            "complete" if complete else "truncated",
        )
        return loaded

    def _slice_schema(self, ref: TableRef, spec: SliceSpec) -> TableSchema | None:
        """Column types for a slice, in the order the source sends them.

        Used when the stream does not announce its own types (an Arrow stream
        does, and those win). Without it an empty slice would land as a table
        with no columns, and the rewritten query would fail on a column that
        simply has no rows.
        """
        if not self._catalog.has_table(ref):
            return None
        schema = self._catalog.get_table(ref)
        if spec.columns is None:
            return schema
        by_name = {c.name: c for c in schema.columns}
        columns = [by_name[name] for name in sorted(spec.columns) if name in by_name]
        return TableSchema(columns=columns) if columns else None

    def estimate_slice(self, ref: TableRef, spec: SliceSpec) -> int | None:
        """Ask the source how many rows a slice would return (None = unknown)."""
        source = self._sources.get(ref.source)
        if source is None:
            return None
        try:
            return source.estimate_slice(ref.table, spec)
        except Exception as e:
            logger.debug("Could not estimate %s %s: %s", ref, spec.describe(), e)
            return None

    def evict_slice(self, table_name: str) -> None:
        """Drop a slice from the store and forget it."""
        self._store.drop_table(table_name)
        dropped = self._catalog.evict_slice(table_name)
        if dropped is not None:
            logger.info("Evicted slice %s (%d rows)", table_name, dropped.row_count)

    def unload(self, ref: TableRef | str) -> None:
        """Drop every slice of a table."""
        table_ref = coerce_ref(ref)
        for loaded in self._catalog.slices_of(table_ref):
            self._store.drop_table(loaded.table_name)
        self._catalog.mark_unloaded(table_ref)

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
        """Bring loaded data up to date and re-read every schema.

        A table with a configured watermark is topped up (only rows newer
        than the highest value already loaded); anything else that is fully
        loaded is re-fetched. Slices that are partial, derived or truncated
        are dropped rather than refreshed: what they hold depends on a query
        that may never be asked again.
        """
        changed = False
        for name, source in list(self._sources.items()):
            try:
                changed |= self._refresh_source(name, source)
                logger.info("Refreshed source: %s", name)
            except Exception as e:
                logger.error("Failed to refresh source %s: %s", name, e)
                if force:
                    raise
        if changed:
            self._data_changed()

    def _refresh_source(self, name: str, source: DataSource) -> bool:
        schema = source.discover_schema()
        changed = False
        counts: dict[str, int] = {}
        for table in schema:
            ref = TableRef(name, table)
            if not self._catalog.is_loaded(ref):
                continue
            spec = self._refresh_specs.get(ref.full_name)
            if spec is not None and spec.is_incremental:
                counts[table] = self._refresh_incrementally(ref, source, spec)
            else:
                counts[table] = self._materialize(ref, SliceSpec.FULL, source).row_count
            changed = True
        changed |= self._drop_stale_slices(name)
        self._catalog.register_source(name, source.source_type, schema)
        for table, count in counts.items():
            schema[table].row_count = count
        return changed

    def _drop_stale_slices(self, name: str) -> bool:
        """Discard slices a refresh cannot bring up to date."""
        dropped = False
        for loaded in self._catalog.all_slices():
            if loaded.ref.source != name:
                continue
            if loaded.is_full and loaded.complete:
                continue
            self.evict_slice(loaded.table_name)
            dropped = True
        return dropped

    def _refresh_incrementally(self, ref: TableRef, source: DataSource, spec: RefreshSpec) -> int:
        """Fetch only rows above the highest watermark already in the store."""
        table_name = ref.full_name
        watermark = self._highest(table_name, spec.watermark_column)
        if watermark is None:
            return self._materialize(ref, SliceSpec.FULL, source).row_count
        slice_spec = SliceSpec(predicates=(Predicate(spec.watermark_column, "gt", watermark),))
        stream = source.fetch_slice(ref.table, slice_spec, max_rows=self._max_ingest_rows or None)
        if spec.key_columns:
            # A row can be updated as well as added, so replace by key
            # instead of appending a second copy of it.
            added = self._store.upsert(table_name, stream, spec.key_columns)
        else:
            added = self._store.append_stream(table_name, stream)
        count = self._store.count(table_name)
        loaded = self._catalog.slices.get(table_name)
        if loaded is not None:
            loaded.row_count = count
            loaded.last_used = self._clock()
            loaded.watermark = self._highest(table_name, spec.watermark_column)
        if self._catalog.has_table(ref):
            self._catalog.set_row_count(ref, count)
        logger.info(
            "Refreshed %s incrementally: %d row(s) after %s > %r",
            ref.full_name,
            added,
            spec.watermark_column,
            watermark,
        )
        return count

    def _highest(self, table_name: str, column: str) -> Any:
        """The largest value of ``column`` currently in the store, or None."""
        if not IDENTIFIER_RE.match(column):
            raise QueryError(f"Invalid watermark column '{column}'")
        table = ".".join(_quote(part) for part in table_name.split(".", 1))
        rows = self._store.execute(f"SELECT MAX({_quote(column)}) FROM {table}")
        return rows.rows[0][0] if rows.rows else None

    def _data_changed(self) -> None:
        """Tell the rest of the application that stored data moved."""
        if self._on_data_changed is not None:
            self._on_data_changed()

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
