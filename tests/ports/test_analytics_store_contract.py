"""Contract every AnalyticsStore implementation must honour."""

import pytest

from fusion.adapters.outbound.duckdb_store import DuckDBStore
from fusion.domain.errors import QueryError
from fusion.domain.models import RowSet, TableRef


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
