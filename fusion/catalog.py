"""Schema catalog for multi-source metadata management."""

from typing import Optional

from fusion.exceptions import SchemaError


class SchemaCatalog:
    """Manages metadata for all connected data sources.

    Tracks table schemas (column names, types, row counts) across multiple
    sources and generates LLM-friendly schema context strings.
    """

    def __init__(self):
        self._sources: dict[str, dict] = {}
        self._loaded_tables: set[str] = set()

    def register_source(self, name: str, source_type: str, tables: dict) -> None:
        """Register a data source with its table schemas.

        Args:
            name: Source name (e.g. "pg_main")
            source_type: Source type (e.g. "postgresql", "mongodb")
            tables: Dict of table_name -> {"columns": [...], "row_count": int}
                    Each column: {"name": str, "type": str, "nullable": bool}
        """
        self._sources[name] = {
            "type": source_type,
            "tables": tables,
        }

    def mark_loaded(self, full_table_name: str) -> None:
        """Mark a table as materialized in DuckDB."""
        self._loaded_tables.add(full_table_name)

    def mark_unloaded(self, full_table_name: str) -> None:
        """Mark a table as not materialized in DuckDB."""
        self._loaded_tables.discard(full_table_name)

    def is_loaded(self, full_table_name: str) -> bool:
        """Check if a table is materialized in DuckDB."""
        return full_table_name in self._loaded_tables

    def list_unloaded_tables(self) -> list[str]:
        """List all tables that are not yet materialized in DuckDB."""
        return [t for t in self.list_tables() if t not in self._loaded_tables]

    def unregister_source(self, name: str) -> None:
        """Remove a source from the catalog."""
        if name in self._sources:
            del self._sources[name]
        self._loaded_tables = {
            t for t in self._loaded_tables if not t.startswith(f"{name}.")
        }

    def get_schema(self, source_name: str) -> dict:
        """Get schema info for a specific source."""
        if source_name not in self._sources:
            raise SchemaError(f"Source '{source_name}' not found in catalog")
        return self._sources[source_name]

    def get_all_schemas(self) -> dict:
        """Get schemas for all registered sources."""
        return dict(self._sources)

    def get_table_info(self, full_table_name: str) -> dict:
        """Get detailed info for a specific table (e.g. 'pg_main.orders')."""
        parts = full_table_name.split(".", 1)
        if len(parts) != 2:
            raise SchemaError(
                f"Invalid table name '{full_table_name}'. Use 'source.table' format."
            )

        source_name, table_name = parts
        if source_name not in self._sources:
            raise SchemaError(f"Source '{source_name}' not found")

        tables = self._sources[source_name]["tables"]
        if table_name not in tables:
            raise SchemaError(f"Table '{table_name}' not found in source '{source_name}'")

        return tables[table_name]

    def list_tables(self) -> list[str]:
        """List all table names across all sources as 'source.table'."""
        result = []
        for source_name, source_info in self._sources.items():
            for table_name in source_info["tables"]:
                result.append(f"{source_name}.{table_name}")
        return result

    def generate_context(self, schemas: Optional[list[str]] = None) -> str:
        """Generate LLM-friendly schema context in Markdown format.

        Args:
            schemas: List of source names to include. None = all sources.
        """
        sources = self._sources
        if schemas:
            sources = {k: v for k, v in sources.items() if k in schemas}

        if not sources:
            return "No schemas available."

        lines = ["# Database Schema\n"]

        for source_name, source_info in sources.items():
            lines.append(f"## Source: {source_name} ({source_info['type']})\n")

            for table_name, table_info in source_info["tables"].items():
                row_count = table_info.get("row_count", "unknown")
                lines.append(f"### {source_name}.{table_name} ({row_count} rows)\n")
                lines.append("| Column | Type | Nullable |")
                lines.append("|--------|------|----------|")

                for col in table_info.get("columns", []):
                    nullable = "YES" if col.get("nullable", True) else "NO"
                    lines.append(f"| {col['name']} | {col['type']} | {nullable} |")

                lines.append("")

        return "\n".join(lines)
