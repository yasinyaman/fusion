"""Tests for the schema catalog."""

import pytest

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import SchemaError
from fusion.domain.models import ColumnInfo, TableRef, TableSchema


@pytest.fixture
def catalog():
    return SchemaCatalog()


def _users(row_count=100):
    return TableSchema([ColumnInfo("id", "int", False), ColumnInfo("name", "varchar")], row_count)


class TestSchemaCatalog:
    def test_register_and_get_source(self, catalog):
        catalog.register_source("pg_main", "postgresql", {"users": _users()})
        entry = catalog.get_source("pg_main")
        assert entry.type == "postgresql"
        assert "users" in entry.tables
        assert catalog.has_source("pg_main")
        assert catalog.source_names() == ["pg_main"]

    def test_get_nonexistent_source(self, catalog):
        with pytest.raises(SchemaError):
            catalog.get_source("nonexistent")
        assert not catalog.has_source("nonexistent")

    def test_unregister_source_clears_loaded_state(self, catalog):
        catalog.register_source("test", "test", {"t1": TableSchema()})
        catalog.mark_loaded("test.t1")
        catalog.unregister_source("test")
        with pytest.raises(SchemaError):
            catalog.get_source("test")
        assert not catalog.is_loaded("test.t1")

    def test_get_table_accepts_ref_or_string(self, catalog):
        catalog.register_source("db", "test", {"users": _users(50)})
        assert catalog.get_table("db.users").row_count == 50
        assert catalog.get_table(TableRef("db", "users")).row_count == 50
        assert catalog.has_table("db.users")
        assert not catalog.has_table("db.nope")

    def test_get_table_invalid_format(self, catalog):
        with pytest.raises(SchemaError):
            catalog.get_table("no_dot_table")

    def test_get_table_unknown_source_or_table(self, catalog):
        catalog.register_source("db", "test", {"users": _users()})
        with pytest.raises(SchemaError):
            catalog.get_table("other.users")
        with pytest.raises(SchemaError):
            catalog.get_table("db.orders")

    def test_set_row_count(self, catalog):
        catalog.register_source("db", "test", {"users": _users(-1)})
        catalog.set_row_count("db.users", 7)
        assert catalog.get_table("db.users").row_count == 7

    def test_list_tables(self, catalog):
        catalog.register_source("src1", "test", {"t1": TableSchema(), "t2": TableSchema()})
        catalog.register_source("src2", "test", {"t3": TableSchema()})
        assert catalog.list_tables() == [
            TableRef("src1", "t1"),
            TableRef("src1", "t2"),
            TableRef("src2", "t3"),
        ]

    def test_loaded_tracking(self, catalog):
        catalog.register_source("src", "test", {"a": TableSchema(), "b": TableSchema()})
        assert catalog.list_unloaded_tables() == [TableRef("src", "a"), TableRef("src", "b")]
        catalog.mark_loaded(TableRef("src", "a"))
        assert catalog.is_loaded("src.a")
        assert catalog.list_unloaded_tables() == [TableRef("src", "b")]
        catalog.mark_unloaded("src.a")
        assert not catalog.is_loaded("src.a")

    def test_generate_context(self, catalog):
        catalog.register_source("pg_main", "postgresql", {"users": _users(100)})
        context = catalog.generate_context()
        assert "pg_main" in context
        assert "users" in context
        assert "100 rows" in context
        assert "| id | int | NO |" in context

    def test_generate_context_unknown_row_count(self, catalog):
        catalog.register_source("s", "t", {"u": TableSchema(row_count=-1)})
        assert "unknown rows" in catalog.generate_context()

    def test_generate_context_filtered(self, catalog):
        catalog.register_source("src1", "test", {"t1": TableSchema(row_count=10)})
        catalog.register_source("src2", "test", {"t2": TableSchema(row_count=20)})
        context = catalog.generate_context(schemas=["src1"])
        assert "src1" in context
        assert "src2" not in context

    def test_generate_context_empty(self, catalog):
        assert "No schemas available" in catalog.generate_context()
