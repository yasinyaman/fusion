"""Fetch strategy for smart, demand-driven data loading.

Analyzes SQL queries or natural language questions to determine which
tables need to be fetched from data sources before execution.
"""

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from fusion.catalog import SchemaCatalog

logger = logging.getLogger(__name__)


@dataclass
class TableTarget:
    """A single table that needs to be fetched."""

    source: str
    table: str

    @property
    def full_name(self) -> str:
        return f"{self.source}.{self.table}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TableTarget):
            return NotImplemented
        return self.source == other.source and self.table == other.table

    def __hash__(self) -> int:
        return hash((self.source, self.table))


@dataclass
class FetchPlan:
    """Plan describing which tables to fetch before query execution."""

    targets: list[TableTarget] = field(default_factory=list)
    strategy_used: str = ""

    # Pushdown eligibility fields (set by FetchStrategy.plan_for_sql)
    is_single_source: bool = False
    source_name: str | None = None
    has_mv_reference: bool = False
    all_targets_unloaded: bool = False

    def add(self, source: str, table: str) -> None:
        """Add a table target, deduplicating."""
        t = TableTarget(source=source, table=table)
        if t not in self.targets:
            self.targets.append(t)

    def is_empty(self) -> bool:
        return len(self.targets) == 0

    @property
    def pushdown_eligible(self) -> bool:
        """Whether this plan is eligible for query pushdown.

        Pushdown requires:
        - At least one table target
        - All tables from a single source
        - No materialized view (mv_*) references
        - All target tables NOT yet loaded in DuckDB (avoid network when local is faster)
        """
        return (
            not self.is_empty()
            and self.is_single_source
            and not self.has_mv_reference
            and self.all_targets_unloaded
        )


class FetchStrategy:
    """Plans which tables to fetch for a given question or SQL query.

    Uses sqlglot AST parsing for SQL inputs and keyword matching for
    natural language questions.
    """

    def __init__(self, catalog: SchemaCatalog):
        self._catalog = catalog

    def plan_for_sql(self, sql: str) -> FetchPlan:
        """Extract table references from a SQL string using sqlglot AST.

        Parses the SQL, walks all Table nodes, and cross-references
        against the catalog to identify real tables (filtering out
        CTE names, subquery aliases, etc.).
        """
        plan = FetchPlan(strategy_used="sql_parse")
        known_tables = set(self._catalog.list_tables())

        try:
            parsed = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            logger.warning("sqlglot failed to parse SQL, returning empty plan")
            return plan

        # Collect CTE names so we can skip them
        cte_names: set[str] = set()
        for statement in (parsed or []):
            if statement is None:
                continue
            for cte in statement.find_all(exp.CTE):
                alias = cte.alias
                if alias:
                    cte_names.add(alias.lower())

        for statement in (parsed or []):
            if statement is None:
                continue
            for table_node in statement.find_all(exp.Table):
                tname = table_node.name
                if not tname:
                    continue

                # Skip CTE references
                if tname.lower() in cte_names:
                    continue

                db = table_node.db  # schema/source prefix

                if db:
                    candidate = f"{db}.{tname}"
                    if candidate in known_tables:
                        plan.add(db, tname)
                else:
                    # Unqualified table: search all sources
                    for full in known_tables:
                        if full.endswith(f".{tname}"):
                            src, _ = full.split(".", 1)
                            plan.add(src, tname)
                            break

        # Compute pushdown eligibility fields
        if not plan.is_empty():
            sources = {t.source for t in plan.targets}
            plan.is_single_source = len(sources) == 1
            plan.source_name = next(iter(sources)) if plan.is_single_source else None

            plan.has_mv_reference = any(
                t.table.startswith("mv_") for t in plan.targets
            )

            plan.all_targets_unloaded = all(
                not self._catalog.is_loaded(t.full_name) for t in plan.targets
            )

        return plan

