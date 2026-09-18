"""FetchPlanner: which tables a statement needs, and how much of each.

``plan_for_sql`` answers the 1.0 question — which catalog tables does this
SQL touch, and can the whole thing be pushed to the source? ``plan_query``
answers the 1.1 one: for each of those tables, is a slice already loaded, is
the table small enough to take whole, can a slice do, or must the query be
refused because the answer would not fit in memory?
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.models import FetchPlan, TableRef
from fusion.domain.policy import (
    MaterializationPolicy,
    QueryPlan,
    SemiJoinSpec,
    TargetPlan,
    budget_message,
    refusal_message,
)
from fusion.domain.query_shape import QueryShape
from fusion.domain.slices import SliceSpec
from fusion.ports.sql_policy import SqlAnalyzer

logger = logging.getLogger(__name__)

SliceEstimator = Callable[[TableRef, SliceSpec], int | None]
"""``(table, slice) -> row count`` as reported by the source, or None."""


class FetchPlanner:
    def __init__(
        self,
        catalog: SchemaCatalog,
        analyzer: SqlAnalyzer,
        policy: MaterializationPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._catalog = catalog
        self._analyzer = analyzer
        self._policy = policy or MaterializationPolicy()
        self._clock = clock

    @property
    def policy(self) -> MaterializationPolicy:
        return self._policy

    def plan_for_sql(self, sql: str) -> FetchPlan:
        """Resolve the analyzer's table references against the catalog.

        Qualified names must match exactly; unqualified names resolve to the
        first source (in registration order) that has a table by that name.
        References to ``mv_*`` tables are noted so pushdown is skipped.
        """
        plan = FetchPlan(strategy_used="sql_parse")
        known = self._catalog.list_tables()
        known_set = set(known)

        for ref in self._analyzer.table_references(sql):
            if ref.is_view:
                plan.has_mv_reference = True
                continue
            if ref.source:
                if ref in known_set:
                    plan.add(ref)
            else:
                for candidate in known:
                    if candidate.table == ref.table:
                        plan.add(candidate)
                        break

        if not plan.is_empty():
            sources = {t.source for t in plan.targets}
            plan.is_single_source = len(sources) == 1
            plan.source_name = next(iter(sources)) if plan.is_single_source else None
            plan.all_targets_unloaded = all(not self._catalog.is_loaded(t) for t in plan.targets)
        return plan

    # -- slice planning -----------------------------------------------------

    def plan_query(self, sql: str, estimator: SliceEstimator | None = None) -> QueryPlan:
        """Decide what to materialize before ``sql`` can run locally."""
        fetch = self.plan_for_sql(sql)
        shape = self._analyzer.analyze(sql)
        if fetch.is_empty():
            return QueryPlan(fetch=fetch, shape=shape)

        targets = [self._plan_target(ref, shape, estimator) for ref in fetch.targets]
        targets = self._add_semi_joins(targets, shape)
        targets = _drivers_first(targets)
        refusal = next((t.reason for t in targets if t.action == "refuse"), "")
        evictions: tuple[str, ...] = ()
        if not refusal:
            evictions, refusal = self._fit_budget(targets)
        return QueryPlan(
            fetch=fetch,
            shape=shape,
            targets=tuple(targets),
            evictions=evictions,
            refusal=refusal,
        )

    def _plan_target(
        self, ref: TableRef, shape: QueryShape, estimator: SliceEstimator | None
    ) -> TargetPlan:
        """Reuse, whole table, slice or refusal — in that order of preference."""
        spec = self._spec_for(ref, shape)
        now = self._clock()
        covering = self._catalog.find_covering_slice(ref, spec, now)
        if covering is not None:
            return TargetPlan(
                ref,
                spec,
                "reuse",
                covering=covering,
                reason=f"already loaded as {covering.table_name}",
            )

        estimate = self._table_estimate(ref)
        if self._policy.allows_full_load(estimate):
            return TargetPlan(ref, SliceSpec.FULL, "load_full", estimate=estimate)

        if not spec.predicates:
            # Selecting fewer columns does not remove a single row, and the
            # budget counts rows: without a filter the slice is the table.
            return TargetPlan(
                ref,
                spec,
                "refuse",
                estimate=estimate,
                slice_estimate=estimate,
                reason=refusal_message(ref, estimate, self._policy),
            )

        slice_estimate = estimator(ref, spec) if estimator is not None else None
        if self._policy.allows_slice(slice_estimate):
            return TargetPlan(
                ref, spec, "load_slice", estimate=estimate, slice_estimate=slice_estimate
            )
        return TargetPlan(
            ref,
            spec,
            "refuse",
            estimate=estimate,
            slice_estimate=slice_estimate,
            reason=refusal_message(
                ref,
                estimate,
                self._policy,
                hints=[
                    f"the current filter still matches about {slice_estimate:,} rows "
                    f"(slice_max_rows={self._policy.slice_max_rows:,}); narrow it further"
                ]
                if slice_estimate is not None
                else (),
            ),
        )

    def _spec_for(self, ref: TableRef, shape: QueryShape) -> SliceSpec:
        """What the query needs of one table (the whole thing when unsure)."""
        if not shape.is_simple_select:
            return SliceSpec.FULL
        use = shape.use_for(ref)
        if use is None:
            return SliceSpec.FULL
        limit = shape.limit if shape.limit_is_pushable else None
        return use.slice_spec(limit=limit)

    def _table_estimate(self, ref: TableRef) -> int | None:
        """Source-side size of a table, as far as the catalog knows it."""
        if not self._catalog.has_table(ref):
            return None
        return self._catalog.get_table(ref).known_estimate

    # -- semi-joins ---------------------------------------------------------

    def _add_semi_joins(self, targets: list[TargetPlan], shape: QueryShape) -> list[TargetPlan]:
        """Turn a refusal into a key-passing fetch when a small table joins to it."""
        if not shape.is_simple_select or not shape.joins:
            return targets
        by_ref = {t.ref: t for t in targets}
        planned = []
        for target in targets:
            if target.action != "refuse":
                planned.append(target)
                continue
            found = self._semi_join_for(target, by_ref, shape)
            semi, driver_rows = found if found is not None else (None, 0)
            planned.append(
                target
                if semi is None
                else TargetPlan(
                    target.ref,
                    target.spec,
                    "semi_join",
                    estimate=target.estimate,
                    # Only the matching rows come back, so the driver's key
                    # count is the size to budget for, not the table's.
                    slice_estimate=driver_rows,
                    semi_join=semi,
                    reason=f"fetched by key from {semi.describe()}",
                )
            )
        return planned

    def _semi_join_for(
        self, target: TargetPlan, by_ref: dict[TableRef, TargetPlan], shape: QueryShape
    ) -> tuple[SemiJoinSpec, int] | None:
        """The key-passing plan for one refused table, plus the driver's size."""
        use = shape.use_for(target.ref)
        if use is None or use.outer_null_side:
            return None
        for join in shape.inner_joins_for(use.alias):
            other = join.other_side(use.alias)
            target_column = join.column_for(use.alias)
            if other is None or target_column is None:
                continue
            driver_use = shape.use_for_alias(other[0])
            if driver_use is None or driver_use.ref == target.ref:
                continue
            driver = by_ref.get(driver_use.ref)
            if driver is None or driver.action == "refuse":
                continue
            driver_rows = self._driver_size(driver)
            if not self._policy.allows_semi_join(driver_rows):
                continue
            return (
                SemiJoinSpec(
                    driver=driver.ref,
                    driver_table=driver.table_name,
                    driver_key=other[1],
                    target_key=target_column,
                    driver_predicates=driver_use.predicates,
                ),
                driver_rows,
            )
        return None

    def _driver_size(self, driver: TargetPlan) -> int:
        """How many key values the driver could contribute (its worst case)."""
        if driver.covering is not None:
            return driver.covering.row_count
        for size in (driver.slice_estimate, driver.estimate):
            if size is not None:
                return size
        return 0  # unknown: the executor counts the real keys and refuses there

    # -- budget -------------------------------------------------------------

    def _fit_budget(self, targets: list[TargetPlan]) -> tuple[tuple[str, ...], str]:
        """Evict least-recently-used slices until this query fits, or refuse."""
        incoming = sum(_expected_rows(t) for t in targets if t.needs_fetch)
        held = self._catalog.slice_rows_total()
        budget = self._policy.slice_budget_rows
        if budget <= 0 or held + incoming <= budget:
            return (), ""

        protect = {t.table_name for t in targets}
        evictions: list[str] = []
        for candidate in self._catalog.lru_slices(protect=protect):
            if held + incoming <= budget:
                break
            evictions.append(candidate.table_name)
            held -= candidate.row_count
        if held + incoming > budget:
            return tuple(evictions), budget_message(held + incoming, self._policy)
        logger.info(
            "Slice budget: evicting %d slice(s) to make room for ~%d rows",
            len(evictions),
            incoming,
        )
        return tuple(evictions), ""

    @staticmethod
    def targets_of(plan: FetchPlan) -> list[TableRef]:
        return list(plan.targets)


def _expected_rows(target: TargetPlan) -> int:
    """Rows a fetch is expected to add (0 when nobody could tell us)."""
    for size in (target.slice_estimate, target.estimate):
        if size is not None:
            return size
    return 0


def _drivers_first(targets: list[TargetPlan]) -> list[TargetPlan]:
    """Order targets so a semi-join's driver is materialized before it is used."""
    ordered: list[TargetPlan] = []
    remaining = list(targets)
    while remaining:
        ready = [
            t
            for t in remaining
            if t.semi_join is None
            or all(other.ref != t.semi_join.driver for other in remaining if other is not t)
        ]
        if not ready:  # a cycle: keep the original order rather than loop forever
            ordered.extend(remaining)
            break
        for target in ready:
            ordered.append(target)
            remaining.remove(target)
    return ordered
