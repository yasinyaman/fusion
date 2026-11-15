"""A Fusion data source backed by the benchmark fixture.

Arm C has to exercise the real pipeline — metric expression, shape derivation,
planner, compiler, DuckDB — so it needs a genuine ``DataSource``. Serving the
fixture rows directly keeps the benchmark independent of a running Warp while
leaving everything above the source untouched.
"""

from __future__ import annotations

from typing import Any

from fusion.domain.models import (
    ColumnInfo,
    RowSet,
    SourceCapabilities,
    TableSchema,
)
from fusion.domain.slices import SliceSpec
from fusion.ports.data_source import fetch_slice_in_memory

# The fixture's column types, as Fusion's catalog would describe them. The
# semantic model is inferred from these, so `tutar` has to read as numeric and
# `islem_tarihi` as a date for a grain to be offered.
SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "musteriler": [
        ("musteri_id", "INTEGER"),
        ("ad_soyad", "VARCHAR"),
        ("eposta", "VARCHAR"),
        ("sube_kodu", "VARCHAR"),
        ("segment", "VARCHAR"),
        ("acilis_tarihi", "DATE"),
    ],
    "islemler": [
        ("islem_id", "INTEGER"),
        ("musteri_id", "INTEGER"),
        ("tutar", "DOUBLE"),
        ("para_birimi", "VARCHAR"),
        ("islem_turu", "VARCHAR"),
        ("durum", "VARCHAR"),
        ("islem_tarihi", "DATE"),
        ("kanal", "VARCHAR"),
    ],
}


class FixtureSource:
    """Serves the benchmark tables to Fusion."""

    source_type = "fixture"

    def __init__(self, name: str, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.name = name
        self._tables = tables
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def discover_schema(self) -> dict[str, TableSchema]:
        return {
            table: TableSchema(
                columns=[ColumnInfo(name=n, type=t) for n, t in SCHEMAS[table]],
                row_count=len(rows),
                row_estimate=len(rows),
            )
            for table, rows in self._tables.items()
        }

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        rows = self._tables[table]
        if max_rows is not None:
            rows = rows[:max_rows]
        # Column order comes from the records, which the fixture builds in
        # schema order, so the RowSet lines up with discover_schema.
        return RowSet.from_records(rows)

    def fetch_slice(self, table: str, spec: SliceSpec, max_rows: int | None = None):
        # The fixture is tiny, so the generic in-memory helper is exactly right:
        # it applies the slice's own predicates and projection.
        return fetch_slice_in_memory(self, table, spec, max_rows)

    def estimate_slice(self, table: str, spec: SliceSpec) -> int | None:
        return sum(1 for row in self._tables[table] if spec.matches(row))

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(pushdown=False, slices=True, arrow=False, row_estimates=True)

    @property
    def supports_pushdown(self) -> bool:
        return False


def fixture_factory(tables: dict[str, list[dict[str, Any]]]):
    """A ``SourceFactory`` serving ``tables``."""

    def create(name: str, config: Any) -> FixtureSource:
        return FixtureSource(name, tables)

    return create
