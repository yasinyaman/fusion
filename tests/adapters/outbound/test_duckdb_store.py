"""Tests for the DuckDB AnalyticsStore adapter."""

import threading

import pytest

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.domain.errors import BackupError, QueryError
from fusion.domain.models import ColumnInfo, RowSet, TableRef, TableSchema

ORDERS = RowSet.from_records(
    [
        {"id": 1, "amount": 10.5, "status": "paid"},
        {"id": 2, "amount": 20.0, "status": None},
    ]
)


@pytest.fixture
def store():
    s = DuckDBStore(threads=1, memory_limit="256MB")
    yield s
    s.close()


def _setting(store, name):
    return store.execute(f"SELECT current_setting('{name}')").rows[0][0]


class TestSecurityLatch:
    def test_external_access_disabled_by_default(self, store):
        assert store.external_access_enabled is False
        assert _setting(store, "enable_external_access") is False
        with pytest.raises(QueryError):
            store.execute("SELECT * FROM read_text('/etc/hosts')")

    def test_external_access_opt_in(self):
        s = DuckDBStore(threads=1, memory_limit="256MB", external_access=True)
        try:
            assert s.external_access_enabled is True
            assert _setting(s, "enable_external_access") is True
        finally:
            s.close()

    def test_settings_applied(self):
        s = DuckDBStore(threads=2, memory_limit="300MB", max_temp_directory_size="1GB")
        try:
            assert _setting(s, "threads") == 2
            assert "MB" in str(_setting(s, "memory_limit")) or "MiB" in str(
                _setting(s, "memory_limit")
            )
        finally:
            s.close()

    def test_invalid_memory_limit_rejected(self):
        with pytest.raises(ValueError):
            DuckDBStore(memory_limit="4GB; DROP")


class TestMaterialize:
    def test_materialize_and_query(self, store):
        store.create_schema("src")
        n = store.materialize(TableRef("src", "orders"), ORDERS)
        assert n == 2
        assert store.count("src.orders") == 2
        cols = {c.name: c for c in store.describe("src.orders")}
        assert cols["id"].type == "BIGINT"
        assert cols["amount"].type == "DOUBLE"
        assert cols["status"].type == "VARCHAR"
        rs = store.execute("SELECT id, status FROM src.orders ORDER BY id")
        assert rs.columns == ("id", "status")
        assert rs.rows == [(1, "paid"), (2, None)]

    def test_materialize_replaces_existing(self, store):
        store.create_schema("src")
        store.materialize(TableRef("src", "t"), ORDERS)
        store.materialize(TableRef("src", "t"), RowSet.from_records([{"id": 9}]))
        assert store.execute("SELECT * FROM src.t").rows == [(9,)]

    def test_materialize_empty_with_schema_creates_typed_table(self, store):
        store.create_schema("src")
        schema = TableSchema([ColumnInfo("id", "integer", False), ColumnInfo("name", "varchar")])
        assert store.materialize(TableRef("src", "empty"), RowSet.empty(), schema) == 0
        assert store.count("src.empty") == 0
        assert [c.name for c in store.describe("src.empty")] == ["id", "name"]
        assert store.execute("SELECT id FROM src.empty WHERE name = 'x'").rows == []

    def test_materialize_empty_without_schema(self, store):
        store.create_schema("src")
        store.materialize(TableRef("src", "empty"), RowSet.empty())
        assert store.count("src.empty") == 0

    def test_mixed_type_column_falls_back_to_varchar(self, store):
        store.create_schema("src")
        rows = RowSet(("v",), [(1,), ("a",), (None,)])
        store.materialize(TableRef("src", "mixed"), rows)
        assert store.describe("src.mixed")[0].type == "VARCHAR"
        assert store.execute("SELECT v FROM src.mixed ORDER BY v NULLS LAST").rows == [
            ("1",),
            ("a",),
            (None,),
        ]

    def test_all_null_column_is_varchar(self, store):
        store.create_schema("src")
        store.materialize(TableRef("src", "nulls"), RowSet(("v",), [(None,), (None,)]))
        assert store.describe("src.nulls")[0].type == "VARCHAR"

    def test_invalid_schema_name(self, store):
        with pytest.raises(QueryError):
            store.create_schema("bad;name")

    def test_drop_schema_cascades(self, store):
        store.create_schema("src")
        store.materialize(TableRef("src", "t"), ORDERS)
        store.drop_schema("src")
        with pytest.raises(QueryError):
            store.count("src.t")


class TestExecute:
    def test_params(self, store):
        rs = store.execute("SELECT ? + 1 AS x, ? AS name", [41, "n"])
        assert rs.rows == [(42, "n")]

    def test_error_wrapped(self, store):
        with pytest.raises(QueryError, match="Query execution failed"):
            store.execute("SELECT * FROM does_not_exist")

    def test_statement_without_result(self, store):
        assert store.execute("SET threads TO 1").is_empty

    def test_create_table_as_only_for_views(self, store):
        store.create_table_as("mv_x", "SELECT 1 AS a")
        assert store.count("mv_x") == 1
        with pytest.raises(QueryError):
            store.create_table_as("plain", "SELECT 1")
        with pytest.raises(QueryError):
            store.create_table_as("mv_bad", "SELECT * FROM nope")
        store.drop_table("mv_x")
        with pytest.raises(QueryError):
            store.count("mv_x")

    def test_concurrent_access_is_serialized(self, store):
        store.create_schema("src")
        store.materialize(TableRef("src", "t"), ORDERS)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for _ in range(20):
                    store.execute("SELECT COUNT(*) FROM src.t")
                    store.materialize(TableRef("src", f"t{i}"), ORDERS)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert store.count("src.t") == 2


class TestBackup:
    def test_database_path(self, store, tmp_path):
        assert store.database_path is None
        s = DuckDBStore(database=str(tmp_path / "f.duckdb"), threads=1, memory_limit="256MB")
        try:
            assert s.database_path == tmp_path / "f.duckdb"
        finally:
            s.close()

    def test_snapshot_in_memory_raises(self, store, tmp_path):
        with pytest.raises(BackupError):
            store.snapshot_to(tmp_path / "x.duckdb")
        with pytest.raises(BackupError):
            store.restore_from(tmp_path / "x.duckdb")

    def test_export_without_external_access_raises(self, store, tmp_path):
        with pytest.raises(BackupError):
            store.export_to(tmp_path / "export")
        with pytest.raises(BackupError):
            store.import_from(tmp_path / "export")

    def test_export_and_import_roundtrip(self, tmp_path):
        src = DuckDBStore(threads=1, memory_limit="256MB", external_access=True)
        src.create_schema("s")
        src.materialize(TableRef("s", "t"), ORDERS)
        target = tmp_path / "export"
        src.export_to(target)
        src.close()
        assert target.is_dir()

        dst = DuckDBStore(threads=1, memory_limit="256MB", external_access=True)
        try:
            dst.import_from(target)
            assert dst.count("s.t") == 2
        finally:
            dst.close()

    def test_restore_from_reapplies_security_latch(self, tmp_path):
        """Regression: reopening after restore must keep external access off."""
        db_file = tmp_path / "live.duckdb"
        s = DuckDBStore(database=str(db_file), threads=1, memory_limit="256MB")
        try:
            s.create_schema("s")
            s.materialize(TableRef("s", "t"), ORDERS)
            backup = tmp_path / "backup.duckdb"
            s.snapshot_to(backup)
            assert backup.exists()

            s.materialize(TableRef("s", "t"), RowSet.from_records([{"id": 99}]))
            assert s.count("s.t") == 1

            s.restore_from(backup)
            assert s.count("s.t") == 2
            assert _setting(s, "enable_external_access") is False
            assert _setting(s, "threads") == 1
            with pytest.raises(QueryError):
                s.execute("SELECT * FROM read_text('/etc/hosts')")
        finally:
            s.close()

    def test_restore_missing_file(self, tmp_path):
        s = DuckDBStore(database=str(tmp_path / "f.duckdb"), threads=1, memory_limit="256MB")
        try:
            with pytest.raises(BackupError):
                s.restore_from(tmp_path / "missing.duckdb")
        finally:
            s.close()
