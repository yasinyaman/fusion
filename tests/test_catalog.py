"""Tests for schema catalog."""

import pytest

from fusion.catalog import SchemaCatalog
from fusion.exceptions import SchemaError


class TestSchemaCatalog:
    def test_register_and_get_source(self, catalog):
        catalog.register_source("pg_main", "postgresql", {
            "users": {"columns": [{"name": "id", "type": "int", "nullable": False}], "row_count": 100},
        })
        schema = catalog.get_schema("pg_main")
        assert schema["type"] == "postgresql"
        assert "users" in schema["tables"]

    def test_get_nonexistent_source(self, catalog):
        with pytest.raises(SchemaError):
            catalog.get_schema("nonexistent")

    def test_unregister_source(self, catalog):
        catalog.register_source("test", "test", {"t1": {"columns": [], "row_count": 0}})
        catalog.unregister_source("test")
        with pytest.raises(SchemaError):
            catalog.get_schema("test")

    def test_get_table_info(self, catalog):
        catalog.register_source("db", "test", {
            "users": {"columns": [{"name": "id", "type": "int", "nullable": False}], "row_count": 50},
        })
        info = catalog.get_table_info("db.users")
        assert info["row_count"] == 50
        assert len(info["columns"]) == 1

    def test_get_table_info_invalid_format(self, catalog):
        with pytest.raises(SchemaError):
            catalog.get_table_info("no_dot_table")

    def test_list_tables(self, catalog):
        catalog.register_source("src1", "test", {"t1": {"columns": []}, "t2": {"columns": []}})
        catalog.register_source("src2", "test", {"t3": {"columns": []}})
        tables = catalog.list_tables()
        assert "src1.t1" in tables
        assert "src1.t2" in tables
        assert "src2.t3" in tables

    def test_generate_context(self, catalog):
        catalog.register_source("pg_main", "postgresql", {
            "users": {
                "columns": [
                    {"name": "id", "type": "int", "nullable": False},
                    {"name": "name", "type": "varchar", "nullable": True},
                ],
                "row_count": 100,
            },
        })
        context = catalog.generate_context()
        assert "pg_main" in context
        assert "users" in context
        assert "100 rows" in context

    def test_generate_context_filtered(self, catalog):
        catalog.register_source("src1", "test", {"t1": {"columns": [], "row_count": 10}})
        catalog.register_source("src2", "test", {"t2": {"columns": [], "row_count": 20}})
        context = catalog.generate_context(schemas=["src1"])
        assert "src1" in context
        assert "src2" not in context

    def test_generate_context_empty(self, catalog):
        context = catalog.generate_context()
        assert "No schemas available" in context
