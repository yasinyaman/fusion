"""Contract every AnalyticsStore implementation must honour."""

import pytest

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.domain.errors import QueryError
from fusion.domain.models import ListRowStream, RowSet, TableRef


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    database = ":memory:" if request.param == "memory" else str(tmp_path / "c.duckdb")
    s = DuckDBStore(database=database, threads=1, memory_limit="256MB")
    yield s
    s.close()


class TestAnalyticsStoreContract:
    def test_database_path(self, store, tmp_path):
        assert store.database_path is None or store.database_path.parent == tmp_path
        assert store.external_access_enabled is False

    def test_schema_materialize_query_lifecycle(self, store):
        ref = TableRef("s", "t")
        store.create_schema("s")
        assert (
            store.materialize(ref, RowSet.from_records([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]))
            == 2
        )
        assert store.count("s.t") == 2
        assert [c.name for c in store.describe("s.t")] == ["a", "b"]
        rs = store.execute("SELECT a FROM s.t WHERE b = ?", ["y"])
        assert rs.columns == ("a",)
        assert rs.rows == [(2,)]
        store.drop_table("s.t")
        with pytest.raises(QueryError):
            store.count("s.t")
        store.drop_schema("s")

    def test_execute_error_is_query_error(self, store):
        with pytest.raises(QueryError):
            store.execute("SELECT * FROM nope")

    def test_create_schema_is_idempotent(self, store):
        store.create_schema("s")
        store.create_schema("s")


class TestStreamingContract:
    """Every store must accept a RowStream and keep the rows it was given."""

    def test_stream_round_trip(self, store):
        store.create_schema("s")
        rows = [{"id": i, "name": f"n{i}"} for i in range(30)]
        assert store.materialize_stream("s.t", ListRowStream.from_records(rows, 7)) == 30
        assert store.count("s.t") == 30
        assert (
            store.append_stream("s.t", ListRowStream.from_records([{"id": 99, "name": "z"}])) == 1
        )
        assert store.table_size("s.t").rows == 31

    def test_upsert_is_idempotent(self, store):
        store.create_schema("s")
        rows = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
        store.materialize_stream("s.t", ListRowStream.from_records(rows))
        for _ in range(2):
            store.upsert("s.t", ListRowStream.from_records([{"id": 2, "v": "B"}]), ["id"])
        assert store.count("s.t") == 2
        assert store.execute("SELECT v FROM s.t WHERE id = 2").rows == [("B",)]

    def test_delete_where_in_and_rename(self, store):
        store.create_schema("s")
        store.materialize_stream(
            "s.staged", ListRowStream.from_records([{"id": 1}, {"id": 2}, {"id": 3}])
        )
        assert store.delete_where_in("s.staged", "id", [2]) == 1
        store.rename_table("s.staged", "s.live")
        assert store.execute("SELECT id FROM s.live ORDER BY id").rows == [(1,), (3,)]
