"""Tests for the schema catalog."""

import pytest

from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import SchemaError
from fusion.domain.models import ColumnInfo, TableRef, TableSchema
from fusion.domain.slices import LoadedSlice, Predicate, SliceSpec


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
        catalog.mark_loaded("pg_main.users", row_count=100)
        context = catalog.generate_context()
        assert "pg_main" in context
        assert "users" in context
        assert "100 rows loaded" in context
        assert "| id | int | NO |" in context

    def test_generate_context_shows_what_is_not_loaded(self, catalog):
        catalog.register_source("s", "t", {"u": TableSchema(row_count=-1)})
        assert "not loaded" in catalog.generate_context()

    def test_generate_context_shows_the_source_estimate(self, catalog):
        catalog.register_source("s", "t", {"u": TableSchema(row_estimate=4_000_000)})
        assert "~4000000 rows, not loaded" in catalog.generate_context()

    def test_generate_context_counts_slice_rows(self, catalog):
        catalog.register_source("s", "t", {"u": TableSchema()})
        catalog.record_slice(
            LoadedSlice(
                ref=TableRef("s", "u"),
                spec=SliceSpec(predicates=(Predicate("a", "eq", 1),)),
                table_name="s.u__s_x",
                row_count=12,
            )
        )
        assert "12 rows loaded in slices" in catalog.generate_context()

    def test_generate_context_filtered(self, catalog):
        catalog.register_source("src1", "test", {"t1": TableSchema(row_count=10)})
        catalog.register_source("src2", "test", {"t2": TableSchema(row_count=20)})
        context = catalog.generate_context(schemas=["src1"])
        assert "src1" in context
        assert "src2" not in context

    def test_generate_context_empty(self, catalog):
        assert "No schemas available" in catalog.generate_context()


class TestSliceTracking:
    """``is_loaded`` means the whole table; slices are tracked separately."""

    @pytest.fixture
    def catalog(self):
        c = SchemaCatalog()
        c.register_source("db", "warp", {"orders": _users(0)})
        return c

    def _slice(self, catalog, **kwargs):
        spec = kwargs.pop("spec", SliceSpec(predicates=(Predicate("status", "eq", "new"),)))
        ref = TableRef("db", "orders")
        return catalog.record_slice(
            LoadedSlice(
                ref=ref,
                spec=spec,
                table_name=spec.table_name_for(ref),
                row_count=kwargs.pop("row_count", 5),
                loaded_at=kwargs.pop("loaded_at", 1.0),
                last_used=kwargs.pop("last_used", 1.0),
                **kwargs,
            )
        )

    def test_mark_loaded_records_a_full_slice(self, catalog):
        catalog.mark_loaded("db.orders", row_count=42, now=7.0)
        assert catalog.is_loaded("db.orders")
        full = catalog.slices_of("db.orders")[0]
        assert full.is_full and full.complete
        assert full.table_name == "db.orders"
        assert (full.row_count, full.loaded_at, full.last_used) == (42, 7.0, 7.0)

    def test_a_partial_slice_does_not_make_the_table_loaded(self, catalog):
        self._slice(catalog)
        assert catalog.is_loaded("db.orders") is False
        assert catalog.list_unloaded_tables() == [TableRef("db", "orders")]
        assert len(catalog.slices_of("db.orders")) == 1

    def test_an_incomplete_full_slice_is_not_loaded(self, catalog):
        self._slice(catalog, spec=SliceSpec.FULL, complete=False)
        assert catalog.is_loaded("db.orders") is False

    def test_find_covering_slice(self, catalog):
        loaded = self._slice(catalog)
        narrower = SliceSpec(
            predicates=(Predicate("status", "eq", "new"), Predicate("total", "gt", 10))
        )
        assert catalog.find_covering_slice("db.orders", narrower, now=3.0) is loaded
        assert loaded.last_used == 3.0
        assert catalog.find_covering_slice("db.orders", SliceSpec.FULL) is None

    def test_eviction_and_totals(self, catalog):
        loaded = self._slice(catalog, row_count=9)
        assert catalog.slice_rows_total() == 9
        assert [s.table_name for s in catalog.lru_slices()] == [loaded.table_name]
        assert catalog.lru_slices(protect=[loaded.table_name]) == []
        assert catalog.evict_slice(loaded.table_name) is loaded
        assert catalog.slices_of("db.orders") == []
        assert catalog.slice_rows_total() == 0

    def test_mark_unloaded_drops_every_slice(self, catalog):
        self._slice(catalog)
        catalog.mark_loaded("db.orders")
        catalog.mark_unloaded("db.orders")
        assert catalog.slices_of("db.orders") == []
        assert catalog.is_loaded("db.orders") is False

    def test_unregister_source_drops_its_slices(self, catalog):
        self._slice(catalog)
        catalog.mark_loaded("db.orders")
        catalog.unregister_source("db")
        assert catalog.all_slices() == []

    def test_touch_updates_last_used(self, catalog):
        loaded = self._slice(catalog)
        catalog.touch_slice(loaded.table_name, 99.0)
        assert loaded.last_used == 99.0
        catalog.touch_slice("nope", 1.0)  # unknown name is a no-op
