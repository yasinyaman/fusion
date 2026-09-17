"""Fetch a large table by the keys a small one already holds.

When a query joins a huge table to a small one, the rows that can possibly
survive the join are exactly those whose key appears in the small table. So
instead of refusing (or dragging the whole table over), Fusion reads the
distinct keys out of the small table and asks the source for just those
rows — the database equivalent of a semi-join, done over HTTP.

The keys go out in chunks, because a single request cannot carry fifty
thousand values. Too many keys and the executor refuses: at that point the
join is not selective enough for this to be cheaper than the whole table.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.models import TableRef, TableSchema
from fusion.domain.policy import MaterializationPolicy, SemiJoinSpec, TargetPlan
from fusion.domain.slices import LoadedSlice, Predicate, SliceSpec
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.data_source import DataSource

logger = logging.getLogger(__name__)

STAGING_SUFFIX = "__tmp"


class SemiJoinExecutor:
    """Loads a slice of a table defined by the keys held in another table."""

    def __init__(
        self,
        store: AnalyticsStore,
        catalog: SchemaCatalog,
        policy: MaterializationPolicy | None = None,
        clock: Callable[[], float] = time.time,
        max_ingest_rows: int = 0,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._policy = policy or MaterializationPolicy()
        self._clock = clock
        self._max_ingest_rows = max_ingest_rows

    def execute(self, target: TargetPlan, source: DataSource) -> LoadedSlice:
        """Materialize ``target`` from its driver's keys; returns the slice."""
        semi = target.semi_join
        if semi is None:
            raise QueryError(f"No semi-join plan for {target.ref.full_name}")
        keys = self._driver_keys(semi)
        if not keys:
            # Nothing can match, but the table still has to exist for the
            # query to run, so load an empty slice with the right columns.
            logger.info("Semi-join on %s: the driver has no keys", target.ref.full_name)
        spec = target.spec.with_predicate(Predicate(semi.target_key, "in", keys))
        table_name = spec.table_name_for(target.ref)

        now = self._clock()
        covering = self._catalog.find_covering_slice(target.ref, spec, now)
        if covering is not None:
            logger.info("Semi-join on %s reuses %s", target.ref.full_name, covering.table_name)
            return covering

        count = self._fetch_in_chunks(target.ref, spec, keys, source, table_name)
        loaded = self._catalog.record_slice(
            LoadedSlice(
                ref=target.ref,
                spec=spec,
                table_name=table_name,
                row_count=count,
                loaded_at=now,
                last_used=now,
                derived_from=f"semijoin:{semi.driver_table}.{semi.driver_key}",
            )
        )
        logger.info(
            "Semi-join loaded %d rows of %s from %d keys in %s",
            count,
            target.ref.full_name,
            len(keys),
            semi.driver_table,
        )
        return loaded

    def _driver_keys(self, semi: SemiJoinSpec) -> tuple[Any, ...]:
        """Distinct non-null key values held by the driver table."""
        if not IDENTIFIER_RE.match(semi.driver_key):
            raise QueryError(f"Invalid join key '{semi.driver_key}'")
        column = '"' + semi.driver_key.replace('"', '""') + '"'
        table = ".".join(
            '"' + part.replace('"', '""') + '"' for part in semi.driver_table.split(".", 1)
        )
        rows = self._store.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
        )
        keys = tuple(row[0] for row in rows.rows)
        if not self._policy.allows_semi_join(len(keys)):
            raise QueryError(
                f"Refusing to fetch {semi.driver_table} keys into the joined table: "
                f"{len(keys):,} distinct values exceed "
                f"semi_join_max_keys={self._policy.semi_join_max_keys:,}. Add a WHERE "
                "condition so fewer keys are passed, or raise "
                "FUSION_SEMI_JOIN_MAX_KEYS."
            )
        return keys

    def _fetch_in_chunks(
        self,
        ref: TableRef,
        spec: SliceSpec,
        keys: Sequence[Any],
        source: DataSource,
        table_name: str,
    ) -> int:
        """Request the matching rows a chunk of keys at a time."""
        staging = f"{table_name}{STAGING_SUFFIX}"
        chunk_size = max(1, self._policy.in_chunk_size)
        chunks = [keys[start : start + chunk_size] for start in range(0, len(keys), chunk_size)]
        max_rows = self._max_ingest_rows or None
        total = 0
        try:
            for index, chunk in enumerate(chunks or [()]):
                remaining = None if max_rows is None else max_rows - total
                if remaining is not None and remaining <= 0:
                    break
                chunk_spec = SliceSpec(
                    columns=spec.columns,
                    predicates=_with_keys(spec, chunk),
                    limit=spec.limit,
                )
                stream = source.fetch_slice(ref.table, chunk_spec, max_rows=remaining)
                if index == 0:
                    total += self._store.materialize_stream(
                        staging, stream, self._slice_schema(ref, spec)
                    )
                else:
                    total += self._store.append_stream(staging, stream)
        except Exception:
            self._store.drop_table(staging)
            raise
        self._store.rename_table(staging, table_name)
        return total

    def _slice_schema(self, ref: TableRef, spec: SliceSpec) -> TableSchema | None:
        """Declared types for the slice, so an empty result still has columns."""
        if not self._catalog.has_table(ref):
            return None
        schema = self._catalog.get_table(ref)
        if spec.columns is None:
            return schema
        by_name = {c.name: c for c in schema.columns}
        columns = [by_name[name] for name in sorted(spec.columns) if name in by_name]
        return TableSchema(columns=columns) if columns else None


def _with_keys(spec: SliceSpec, chunk: Sequence[Any]) -> tuple[Predicate, ...]:
    """The slice's own conditions, with the key list narrowed to this chunk."""
    key_predicate = spec.predicates[-1]
    return (*spec.predicates[:-1], Predicate(key_predicate.column, "in", chunk))
