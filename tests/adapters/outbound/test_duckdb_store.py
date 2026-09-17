"""Tests for the DuckDB AnalyticsStore adapter."""

import threading

import pyarrow as pa
import pytest

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.domain.errors import BackupError, QueryError
from fusion.domain.models import ColumnInfo, ListRowStream, RowSet, TableRef, TableSchema


class _ArrowStream:
    """RowStream whose fast path hands the store an Arrow reader."""

    def __init__(self, reader):
        self._reader = reader
        self.iterated = False

    @property
    def columns(self):
        return tuple(self._reader.schema.names)

    @property
    def schema(self):
        return None

    def arrow_reader(self):
        return self._reader

    def __iter__(self):
        self.iterated = True
        return iter(())

    def close(self):
        return None


class _ClosingStream:
    """RowStream over fixed batches that records whether it was closed."""

    def __init__(self, batches):
        self._batches = batches
        self.closed = False

    @property
    def columns(self):
        return self._batches[0].columns if self._batches else ()

    @property
    def schema(self):
        return None

    def arrow_reader(self):
        return None

    def __iter__(self):
        return iter(self._batches)

    def close(self):
        self.closed = True


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


class TestStreamingIngest:
    """Streams are written batch by batch, so a table can exceed memory."""

    def test_materialize_stream_writes_every_batch(self, store):
        store.create_schema("s")
        stream = ListRowStream.from_records(
            [{"id": i, "name": f"n{i}"} for i in range(250)], batch_size=100
        )
        assert store.materialize_stream("s.t", stream) == 250
        assert store.count("s.t") == 250
        assert store.execute("SELECT MAX(id) FROM s.t").rows == [(249,)]

    def test_materialize_stream_replaces_previous_content(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}, {"id": 2}]))
        assert store.materialize_stream("s.t", ListRowStream.from_records([{"id": 9}])) == 1
        assert store.execute("SELECT id FROM s.t").rows == [(9,)]

    def test_empty_stream_creates_a_typed_empty_table(self, store):
        store.create_schema("s")
        schema = TableSchema([ColumnInfo("id", "integer"), ColumnInfo("name", "varchar")])
        assert store.materialize_stream("s.t", ListRowStream(RowSet.empty()), schema) == 0
        assert store.count("s.t") == 0
        assert [c.name for c in store.describe("s.t")] == ["id", "name"]

    def test_empty_stream_without_a_schema_still_leaves_a_table(self, store):
        store.create_schema("s")
        assert store.materialize_stream("s.t", ListRowStream(RowSet.empty())) == 0
        assert store.count("s.t") == 0

    def test_declared_schema_types_win_over_the_first_batch(self, store):
        store.create_schema("s")
        schema = TableSchema([ColumnInfo("id", "integer"), ColumnInfo("code", "varchar")])
        stream = ListRowStream.from_records(
            [{"id": 1, "code": "10"}, {"id": 2, "code": "abc"}], batch_size=1
        )
        assert store.materialize_stream("s.t", stream, schema) == 2
        types = {c.name: c.type for c in store.describe("s.t")}
        assert types["id"] == "BIGINT" and types["code"] == "VARCHAR"
        assert store.execute("SELECT code FROM s.t ORDER BY id").rows == [("10",), ("abc",)]

    def test_arrow_reader_is_used_when_the_stream_offers_one(self, store):
        store.create_schema("s")
        table = pa.table({"id": [1, 2, 3], "amount": [1.5, 2.5, 3.5]})
        stream = _ArrowStream(table.to_reader(max_chunksize=2))
        assert store.materialize_stream("s.t", stream) == 3
        assert store.count("s.t") == 3
        assert stream.iterated is False  # the reader was handed over, not iterated
        assert store.execute("SELECT SUM(amount) FROM s.t").rows == [(7.5,)]

    def test_append_stream_adds_to_an_existing_table(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}]))
        assert store.append_stream("s.t", ListRowStream.from_records([{"id": 2}, {"id": 3}])) == 2
        assert store.execute("SELECT id FROM s.t ORDER BY id").rows == [(1,), (2,), (3,)]

    def test_append_stream_creates_the_table_when_missing(self, store):
        store.create_schema("s")
        assert store.append_stream("s.t", ListRowStream.from_records([{"id": 1}])) == 1
        assert store.count("s.t") == 1

    def test_later_batches_are_cast_into_the_existing_columns(self, store):
        store.create_schema("s")
        stream = _ClosingStream(
            [
                RowSet.from_records([{"id": 1}]),
                # A wider batch (an extra column) and a differently-typed but
                # convertible value both land in the table as it already is.
                RowSet.from_records([{"id": "2", "extra": "ignored"}]),
            ]
        )
        assert store.materialize_stream("s.t", stream) == 2
        assert store.execute("SELECT id FROM s.t ORDER BY id").rows == [(1,), (2,)]
        assert [c.name for c in store.describe("s.t")] == ["id"]

    def test_stream_is_closed_even_when_a_batch_cannot_be_written(self, store):
        store.create_schema("s")
        stream = _ClosingStream(
            [RowSet.from_records([{"id": 1}]), RowSet.from_records([{"id": "not a number"}])]
        )
        with pytest.raises(QueryError, match="batch"):
            store.materialize_stream("s.t", stream)
        assert stream.closed


class TestUpsert:
    def test_replaces_matching_rows_and_inserts_the_rest(self, store):
        store.create_schema("s")
        store.materialize_stream(
            "s.t", ListRowStream.from_records([{"id": 1, "v": "a"}, {"id": 2, "v": "b"}])
        )
        changed = ListRowStream.from_records([{"id": 2, "v": "B"}, {"id": 3, "v": "c"}])
        assert store.upsert("s.t", changed, ["id"]) == 2
        assert store.execute("SELECT id, v FROM s.t ORDER BY id").rows == [
            (1, "a"),
            (2, "B"),
            (3, "c"),
        ]

    def test_composite_keys(self, store):
        store.create_schema("s")
        rows = [{"a": 1, "b": 1, "v": "x"}, {"a": 1, "b": 2, "v": "y"}]
        store.materialize_stream("s.t", ListRowStream.from_records(rows))
        store.upsert("s.t", ListRowStream.from_records([{"a": 1, "b": 2, "v": "Y"}]), ["a", "b"])
        assert store.execute("SELECT v FROM s.t ORDER BY b").rows == [("x",), ("Y",)]

    def test_creates_the_table_when_it_does_not_exist(self, store):
        store.create_schema("s")
        assert store.upsert("s.t", ListRowStream.from_records([{"id": 1}]), ["id"]) == 1
        assert store.count("s.t") == 1

    def test_staging_table_is_always_removed(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}]))
        store.upsert("s.t", ListRowStream.from_records([{"id": 1}]), ["id"])
        with pytest.raises(QueryError):
            store.count("s.t__stage")

    def test_without_key_columns_is_refused(self, store):
        store.create_schema("s")
        with pytest.raises(QueryError, match="key column"):
            store.upsert("s.t", ListRowStream.from_records([{"id": 1}]), [])

    def test_unknown_key_column_is_a_query_error(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}]))
        with pytest.raises(QueryError, match="Upsert"):
            store.upsert("s.t", ListRowStream.from_records([{"id": 1}]), ["nope"])
        with pytest.raises(QueryError):
            store.count("s.t__stage")  # still cleaned up


class TestDeleteAndSize:
    def test_delete_where_in(self, store):
        store.create_schema("s")
        store.materialize_stream(
            "s.t", ListRowStream.from_records([{"id": i} for i in range(1, 6)])
        )
        assert store.delete_where_in("s.t", "id", [2, 4, 99]) == 2
        assert store.execute("SELECT id FROM s.t ORDER BY id").rows == [(1,), (3,), (5,)]

    def test_delete_where_in_with_no_values_is_a_no_op(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}]))
        assert store.delete_where_in("s.t", "id", []) == 0
        assert store.count("s.t") == 1

    def test_delete_where_in_unknown_column(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}]))
        with pytest.raises(QueryError, match="Delete"):
            store.delete_where_in("s.t", "nope", [1])

    def test_table_size(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": i} for i in range(100)]))
        size = store.table_size("s.t")
        assert size.rows == 100
        assert size.bytes is None or size.bytes >= 0

    def test_table_size_of_an_unknown_table_raises(self, store):
        with pytest.raises(QueryError):
            store.table_size("s.nope")


class TestRenameTable:
    def test_publishes_a_staged_load(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t__tmp", ListRowStream.from_records([{"id": 1}]))
        store.rename_table("s.t__tmp", "s.t")
        assert store.count("s.t") == 1
        with pytest.raises(QueryError):
            store.count("s.t__tmp")

    def test_replaces_the_existing_target(self, store):
        store.create_schema("s")
        store.materialize_stream("s.t", ListRowStream.from_records([{"id": 1}, {"id": 2}]))
        store.materialize_stream("s.t__tmp", ListRowStream.from_records([{"id": 9}]))
        store.rename_table("s.t__tmp", "s.t")
        assert store.execute("SELECT id FROM s.t").rows == [(9,)]

    def test_bare_target_name_stays_in_the_same_schema(self, store):
        store.create_schema("s")
        store.materialize_stream("s.a", ListRowStream.from_records([{"id": 1}]))
        store.rename_table("s.a", "b")
        assert store.count("s.b") == 1

    def test_cross_schema_rename_is_refused(self, store):
        store.create_schema("s")
        store.create_schema("other")
        store.materialize_stream("s.a", ListRowStream.from_records([{"id": 1}]))
        with pytest.raises(QueryError, match="across schemas"):
            store.rename_table("s.a", "other.a")

    def test_missing_source_table_is_a_query_error(self, store):
        store.create_schema("s")
        with pytest.raises(QueryError, match="rename"):
            store.rename_table("s.nope", "s.other")


def test_slice_table_names_with_dots_are_quoted_correctly(store):
    # A slice table is ``schema.table__s_<hash>``; only the first dot splits.
    store.create_schema("db")
    store.materialize_stream("db.orders__s_ab12cd34ef", ListRowStream.from_records([{"id": 1}]))
    assert store.count("db.orders__s_ab12cd34ef") == 1
    assert store.execute('SELECT id FROM "db"."orders__s_ab12cd34ef"').rows == [(1,)]
