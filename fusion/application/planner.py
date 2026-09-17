"""FetchPlanner: which catalog tables a SQL statement needs, and pushdown eligibility."""

from __future__ import annotations

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.models import FetchPlan, TableRef
from fusion.ports.sql_policy import SqlAnalyzer


class FetchPlanner:
    def __init__(self, catalog: SchemaCatalog, analyzer: SqlAnalyzer) -> None:
        self._catalog = catalog
        self._analyzer = analyzer

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

    @staticmethod
    def targets_of(plan: FetchPlan) -> list[TableRef]:
        return list(plan.targets)
