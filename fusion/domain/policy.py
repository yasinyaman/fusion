"""How much data Fusion is willing to move, and what to do when that is too much.

The engine is an in-memory store: a source table larger than the machine can
hold must not be loaded just because a query mentioned it. The policy turns
that judgement into numbers, and every refusal comes with the concrete ways
out (add a WHERE, select fewer columns, join to a small table, raise the
limit).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fusion.domain.models import FetchPlan, TableRef
from fusion.domain.query_shape import QueryShape
from fusion.domain.slices import LoadedSlice, SliceSpec

Action = Literal["reuse", "load_full", "load_slice", "semi_join", "refuse"]


@dataclass(frozen=True, slots=True)
class MaterializationPolicy:
    """Row budgets for loading source data into the analytics store."""

    #: Largest table Fusion will load whole when a query has no usable filter.
    full_load_max_rows: int = 500_000
    #: Largest slice (filtered/projected read) Fusion will pull in one go.
    slice_max_rows: int = 500_000
    #: Total rows kept across all slices before the least used ones are dropped.
    slice_budget_rows: int = 2_000_000
    #: Most join keys passed to the source in a semi-join.
    semi_join_max_keys: int = 50_000
    #: How many keys go into one ``IN (...)`` request.
    in_chunk_size: int = 1_000

    def allows_full_load(self, estimate: int | None) -> bool:
        """An unknown estimate is allowed: the source could not tell us."""
        return estimate is None or estimate <= self.full_load_max_rows

    def allows_slice(self, estimate: int | None) -> bool:
        return estimate is None or estimate <= self.slice_max_rows

    def allows_semi_join(self, key_count: int) -> bool:
        return key_count <= self.semi_join_max_keys

    def as_dict(self) -> dict[str, int]:
        return {
            "full_load_max_rows": self.full_load_max_rows,
            "slice_max_rows": self.slice_max_rows,
            "slice_budget_rows": self.slice_budget_rows,
            "semi_join_max_keys": self.semi_join_max_keys,
            "in_chunk_size": self.in_chunk_size,
        }


@dataclass(frozen=True, slots=True)
class SemiJoinSpec:
    """Fetch only the rows of the target whose key appears in ``driver``."""

    driver: TableRef
    driver_table: str
    driver_key: str
    target_key: str

    def describe(self) -> str:
        return f"{self.driver.full_name}.{self.driver_key} -> {self.target_key}"


@dataclass(frozen=True, slots=True)
class TargetPlan:
    """What to do about one table a query reads."""

    ref: TableRef
    spec: SliceSpec = SliceSpec.FULL
    action: Action = "load_full"
    covering: LoadedSlice | None = None
    estimate: int | None = None
    slice_estimate: int | None = None
    reason: str = ""
    semi_join: SemiJoinSpec | None = None

    @property
    def table_name(self) -> str:
        """Where the data for this target lives (or will live) in the store."""
        if self.covering is not None:
            return self.covering.table_name
        return self.spec.table_name_for(self.ref)

    @property
    def needs_fetch(self) -> bool:
        return self.action in ("load_full", "load_slice", "semi_join")

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.ref.full_name,
            "action": self.action,
            "target_table": self.table_name,
            "slice": self.spec.describe(),
            "estimate": self.estimate,
            "slice_estimate": self.slice_estimate,
            "reason": self.reason,
            "semi_join": self.semi_join.describe() if self.semi_join else None,
        }


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Everything the query pipeline needs before it can execute a statement.

    ``targets`` says what to do about each table, ``evictions`` names slices
    that have to go first to stay inside the budget, and a non-empty
    ``refusal`` means the query must not run at all.
    """

    fetch: FetchPlan
    shape: QueryShape
    targets: tuple[TargetPlan, ...] = ()
    evictions: tuple[str, ...] = ()
    refusal: str = ""

    @property
    def is_refused(self) -> bool:
        return bool(self.refusal)

    @property
    def fetches(self) -> tuple[TargetPlan, ...]:
        return tuple(t for t in self.targets if t.needs_fetch)

    def table_mapping(self) -> dict[TableRef, str]:
        """Tables whose name in the store differs from the reference in the SQL."""
        return {t.ref: t.table_name for t in self.targets if t.table_name != t.ref.full_name}

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": [t.as_dict() for t in self.targets],
            "evictions": list(self.evictions),
            "refusal": self.refusal,
            "shape": self.shape.as_dict(),
        }


def refusal_message(
    ref: TableRef,
    estimate: int | None,
    policy: MaterializationPolicy,
    hints: Sequence[str] = (),
) -> str:
    """Why a table was not loaded, and what the caller can do about it."""
    size = f"{estimate:,} rows estimated" if estimate is not None else "size unknown"
    lines = [
        f"Refusing to load {ref.full_name} ({size}): it exceeds "
        f"full_load_max_rows={policy.full_load_max_rows:,}.",
        "Try one of:",
        f"  1. add a WHERE condition on {ref.table} columns "
        "(=, !=, <, <=, >, >=, LIKE, IN, IS NULL against a literal)",
        "  2. select only the columns you need instead of *",
        "  3. join it to a smaller table on an equality key, so only the matching rows are fetched",
        f"  4. load a slice explicitly: load_table('{ref.full_name}', where='...', columns=[...])",
    ]
    lines.extend(f"  {i}. {hint}" for i, hint in enumerate(hints, start=len(lines) - 1))
    lines.append(
        "Raise FUSION_FULL_LOAD_MAX_ROWS (and FUSION_SLICE_MAX_ROWS) to allow "
        "bigger loads if the machine has the memory."
    )
    return "\n".join(lines)


def budget_message(needed: int, policy: MaterializationPolicy) -> str:
    """Why a query was refused after eviction could not free enough room."""
    return (
        f"Slice budget exhausted: this query needs about {needed:,} rows in the "
        f"store but slice_budget_rows={policy.slice_budget_rows:,}. Narrow the "
        "query (more selective WHERE, fewer columns) or raise "
        "FUSION_SLICE_BUDGET_ROWS."
    )
