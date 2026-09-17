"""Schema catalog: metadata for every connected source and its tables."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from fusion.domain.errors import SchemaError
from fusion.domain.models import TableRef, TableSchema, coerce_ref


@dataclass(slots=True)
class SourceEntry:
    """A registered source: its type and table schemas."""

    type: str
    tables: dict[str, TableSchema] = field(default_factory=dict)


class SchemaCatalog:
    """Tracks table schemas across sources and which tables are loaded.

    "Loaded" means materialized in the analytics store; the catalog itself
    never touches the store.
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceEntry] = {}
        self._loaded: set[TableRef] = set()

    # -- registration -------------------------------------------------------

    def register_source(
        self, name: str, source_type: str, tables: Mapping[str, TableSchema]
    ) -> None:
        self._sources[name] = SourceEntry(type=source_type, tables=dict(tables))

    def unregister_source(self, name: str) -> None:
        self._sources.pop(name, None)
        self._loaded = {ref for ref in self._loaded if ref.source != name}

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
        return [ref for ref in self.list_tables() if ref not in self._loaded]

    # -- load state ---------------------------------------------------------

    def mark_loaded(self, ref: TableRef | str) -> None:
        self._loaded.add(coerce_ref(ref))

    def mark_unloaded(self, ref: TableRef | str) -> None:
        self._loaded.discard(coerce_ref(ref))

    def is_loaded(self, ref: TableRef | str) -> bool:
        return coerce_ref(ref) in self._loaded

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
                row_count: int | str = schema.row_count if schema.row_count >= 0 else "unknown"
                lines.append(f"### {source_name}.{table_name} ({row_count} rows)\n")
                lines.append("| Column | Type | Nullable |")
                lines.append("|--------|------|----------|")
                for col in schema.columns:
                    nullable = "YES" if col.nullable else "NO"
                    lines.append(f"| {col.name} | {col.type} | {nullable} |")
                lines.append("")
        return "\n".join(lines)
