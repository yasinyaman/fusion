"""Schema catalog: metadata for every connected source and its tables."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fusion.domain.errors import SchemaError
from fusion.domain.models import TableRef, TableSchema, coerce_ref
from fusion.domain.slices import LoadedSlice, SliceRegistry, SliceSpec


@dataclass(slots=True)
class SourceEntry:
    """A registered source: its type and table schemas."""

    type: str
    tables: dict[str, TableSchema] = field(default_factory=dict)


class SchemaCatalog:
    """Tracks table schemas across sources and what is materialized.

    A table is "loaded" when its *whole* content sits in the analytics store.
    Partial reads are tracked as slices (see ``fusion.domain.slices``): a
    table with only slices is not loaded, and queries that a slice cannot
    answer still go back to the source. The catalog never touches the store.
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceEntry] = {}
        self._slices = SliceRegistry()

    # -- registration -------------------------------------------------------

    def register_source(
        self, name: str, source_type: str, tables: Mapping[str, TableSchema]
    ) -> None:
        self._sources[name] = SourceEntry(type=source_type, tables=dict(tables))

    def unregister_source(self, name: str) -> None:
        self._sources.pop(name, None)
        self._slices.evict_source(name)

    # -- lookups ------------------------------------------------------------

    def source_names(self) -> list[str]:
        return list(self._sources)

    def has_source(self, name: str) -> bool:
        return name in self._sources

    def get_source(self, name: str) -> SourceEntry:
        try:
            return self._sources[name]
        except KeyError:
            raise SchemaError(f"Source '{name}' not found in catalog") from None

    def get_all_sources(self) -> dict[str, SourceEntry]:
        return dict(self._sources)

    def has_table(self, ref: TableRef | str) -> bool:
        try:
            self.get_table(ref)
        except SchemaError:
            return False
        return True

    def get_table(self, ref: TableRef | str) -> TableSchema:
        table_ref = coerce_ref(ref)
        if table_ref.source not in self._sources:
            raise SchemaError(f"Source '{table_ref.source}' not found")
        tables = self._sources[table_ref.source].tables
        if table_ref.table not in tables:
            raise SchemaError(f"Table '{table_ref.table}' not found in source '{table_ref.source}'")
        return tables[table_ref.table]

    def set_row_count(self, ref: TableRef | str, row_count: int) -> None:
        self.get_table(ref).row_count = row_count

    def list_tables(self) -> list[TableRef]:
        return [
            TableRef(source_name, table_name)
            for source_name, entry in self._sources.items()
            for table_name in entry.tables
        ]

    def list_unloaded_tables(self) -> list[TableRef]:
        return [ref for ref in self.list_tables() if not self.is_loaded(ref)]

    # -- load state ---------------------------------------------------------

    def mark_loaded(self, ref: TableRef | str, row_count: int = 0, now: float = 0.0) -> None:
        """Record that the whole table is in the store."""
        table_ref = coerce_ref(ref)
        self._slices.record(
            LoadedSlice(
                ref=table_ref,
                spec=SliceSpec.FULL,
                table_name=table_ref.full_name,
                row_count=row_count,
                loaded_at=now,
                last_used=now,
            )
        )

    def mark_unloaded(self, ref: TableRef | str) -> None:
        """Forget every slice of a table (it is gone from the store)."""
        self._slices.evict_ref(coerce_ref(ref))

    def is_loaded(self, ref: TableRef | str) -> bool:
        """True only for a complete whole-table slice."""
        return self._slices.full_slice(coerce_ref(ref)) is not None

    # -- slices -------------------------------------------------------------

    @property
    def slices(self) -> SliceRegistry:
        """The slice registry (planner and source service work through it)."""
        return self._slices

    def record_slice(self, loaded: LoadedSlice) -> LoadedSlice:
        return self._slices.record(loaded)

    def find_covering_slice(
        self, ref: TableRef | str, spec: SliceSpec, now: float = 0.0
    ) -> LoadedSlice | None:
        return self._slices.find_covering(coerce_ref(ref), spec, now)

    def slices_of(self, ref: TableRef | str) -> list[LoadedSlice]:
        return self._slices.slices(coerce_ref(ref))

    def all_slices(self) -> list[LoadedSlice]:
        return self._slices.all()

    def evict_slice(self, table_name: str) -> LoadedSlice | None:
        return self._slices.evict(table_name)

    def touch_slice(self, table_name: str, now: float) -> None:
        self._slices.touch(table_name, now)

    def slice_rows_total(self) -> int:
        return self._slices.total_rows()

    def lru_slices(self, protect: Iterable[str] = ()) -> list[LoadedSlice]:
        """Slices in eviction order (least recently used first)."""
        return self._slices.lru_candidates(protect)

    # -- LLM context --------------------------------------------------------

    def generate_context(self, schemas: list[str] | None = None) -> str:
        """Markdown description of the selected (or all) sources."""
        sources = self._sources
        if schemas:
            sources = {k: v for k, v in sources.items() if k in schemas}
        if not sources:
            return "No schemas available."

        lines = ["# Database Schema\n"]
        for source_name, entry in sources.items():
            lines.append(f"## Source: {source_name} ({entry.type})\n")
            for table_name, schema in entry.tables.items():
                ref = TableRef(source_name, table_name)
                loaded = schema.row_count if schema.row_count >= 0 else 0
                estimate = schema.row_estimate
                if self.is_loaded(ref):
                    size = f"{loaded} rows loaded"
                elif self.slices_of(ref):
                    slice_rows = sum(s.row_count for s in self.slices_of(ref))
                    size = f"{slice_rows} rows loaded in slices"
                elif estimate >= 0:
                    size = f"~{estimate} rows, not loaded"
                else:
                    size = "not loaded"
                lines.append(f"### {source_name}.{table_name} ({size})\n")
                lines.append("| Column | Type | Nullable |")
                lines.append("|--------|------|----------|")
                for col in schema.columns:
                    nullable = "YES" if col.nullable else "NO"
                    lines.append(f"| {col.name} | {col.type} | {nullable} |")
                lines.append("")
        return "\n".join(lines)
