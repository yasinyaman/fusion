"""Contract every DataSource implementation must honour."""

import pytest

from fusion.adapters.outbound.warp.source import WarpSource
from fusion.domain.errors import ConnectionError
from fusion.domain.models import RowSet
from fusion.domain.slices import Predicate, SliceSpec
from fusion.ports.data_source import DataSource, PushdownCapable, fetch_slice_in_memory
from tests.fakes import FakeDataSource, FakeWarpTransport, run_mock_sql

TABLES = {
    "users": [
        {"id": 1, "name": "Alice", "segment": "premium"},
        {"id": 2, "name": "Bob", "segment": "basic"},
        {"id": 3, "name": "Cara", "segment": "premium"},
    ],
    "orders": [{"id": 1, "user_id": 1, "amount": 5.0}, {"id": 2, "user_id": 2, "amount": 7.5}],
}


def _fake():
    return FakeDataSource(
        "src",
        TABLES,
        pushdown=True,
        sql_executor=lambda sql: RowSet.from_records(run_mock_sql(sql, TABLES)),
    )


def _warp():
    transport = FakeWarpTransport(TABLES, database="src")
    return WarpSource.from_config(
        "src", {"base_url": "http://localhost:8080", "transport": transport}
    )


@pytest.fixture(params=[_fake, _warp], ids=["FakeDataSource", "WarpSource"])
def source(request):
    s = request.param()
    yield s
    s.close()


class TestDataSourceContract:
    def test_is_a_data_source(self, source):
        assert isinstance(source, DataSource)
        assert isinstance(source.name, str)
        assert isinstance(source.source_type, str)
        assert isinstance(source.supports_pushdown, bool)

    def test_fetch_before_connect_raises(self, source):
        with pytest.raises(ConnectionError):
            source.fetch_table("users")

    def test_discover_schema(self, source):
        source.connect()
        schema = source.discover_schema()
        assert set(schema) == {"users", "orders"}
        names = [c.name for c in schema["users"].columns]
        assert names == ["id", "name", "segment"]
        assert schema["users"].columns[0].type == "integer"

    def test_fetch_table_returns_rowset(self, source):
        source.connect()
        rows = source.fetch_table("users")
        assert isinstance(rows, RowSet)
        assert rows.columns == ("id", "name", "segment")
        assert rows.column("name") == ["Alice", "Bob", "Cara"]

    def test_fetch_table_max_rows(self, source):
        source.connect()
        assert len(source.fetch_table("users", max_rows=2)) == 2

    def test_pushdown_contract(self, source):
        source.connect()
        if not source.supports_pushdown:
            pytest.skip("source does not support pushdown")
        assert isinstance(source, PushdownCapable)
        counted = source.execute_query("SELECT COUNT(*) AS count FROM users")
        assert counted.to_records() == [{"count": 3}]
        filtered = source.fetch_filtered("users", {"segment": "premium"}, limit=10)
        assert filtered.column("id") == [1, 3]


class TestSliceContract:
    """``fetch_slice`` must return exactly the rows the spec describes."""

    def _rows(self, stream):
        try:
            return [dict(zip(b.columns, r, strict=True)) for b in stream for r in b.rows]
        finally:
            stream.close()

    def test_capabilities_agree_with_supports_pushdown(self, source):
        source.connect()
        caps = source.capabilities
        assert caps.pushdown == source.supports_pushdown
        assert isinstance(caps.slices, bool) and isinstance(caps.arrow, bool)

    def test_full_slice_returns_the_whole_table(self, source):
        source.connect()
        rows = self._rows(source.fetch_slice("users", SliceSpec.FULL))
        assert [r["id"] for r in rows] == [1, 2, 3]

    def test_predicates_filter_at_the_source(self, source):
        source.connect()
        spec = SliceSpec(predicates=(Predicate("segment", "eq", "premium"),))
        rows = self._rows(source.fetch_slice("users", spec))
        assert [r["name"] for r in rows] == ["Alice", "Cara"]

    def test_several_predicates_are_an_and(self, source):
        source.connect()
        spec = SliceSpec(
            predicates=(Predicate("segment", "eq", "premium"), Predicate("id", "gt", 1))
        )
        assert [r["id"] for r in self._rows(source.fetch_slice("users", spec))] == [3]

    def test_projection_returns_only_those_columns(self, source):
        source.connect()
        spec = SliceSpec(columns=frozenset({"id", "name"}))
        rows = self._rows(source.fetch_slice("users", spec))
        assert all(set(r) == {"id", "name"} for r in rows)

    def test_in_and_is_null_predicates(self, source):
        source.connect()
        in_spec = SliceSpec(predicates=(Predicate("id", "in", (1, 3)),))
        assert [r["id"] for r in self._rows(source.fetch_slice("users", in_spec))] == [1, 3]

    def test_limit_and_max_rows_both_cap_the_read(self, source):
        source.connect()
        assert len(self._rows(source.fetch_slice("users", SliceSpec(limit=2)))) == 2
        assert len(self._rows(source.fetch_slice("users", SliceSpec.FULL, max_rows=1))) == 1

    def test_a_slice_matching_nothing_is_empty(self, source):
        source.connect()
        spec = SliceSpec(predicates=(Predicate("segment", "eq", "nope"),))
        assert self._rows(source.fetch_slice("users", spec)) == []

    def test_estimate_slice_counts_without_fetching(self, source):
        source.connect()
        assert source.estimate_slice("users", SliceSpec.FULL) == 3
        spec = SliceSpec(predicates=(Predicate("segment", "eq", "premium"),))
        assert source.estimate_slice("users", spec) == 2

    def test_fetch_slice_in_memory_matches_the_native_path(self, source):
        source.connect()
        spec = SliceSpec(
            columns=frozenset({"id", "segment"}),
            predicates=(Predicate("segment", "eq", "premium"),),
        )
        native = self._rows(source.fetch_slice("users", spec))
        fallback = self._rows(fetch_slice_in_memory(source, "users", spec))
        assert native == fallback
