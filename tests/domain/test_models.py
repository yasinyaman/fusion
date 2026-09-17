"""Tests for pure domain models."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fusion.domain.errors import SchemaError
from fusion.domain.models import (
    BackupInfo,
    ColumnInfo,
    FetchPlan,
    QueryResult,
    RowSet,
    TableRef,
    TableSchema,
    coerce_ref,
)


class TestTableRef:
    def test_full_name_and_str(self):
        ref = TableRef("db", "orders")
        assert ref.full_name == "db.orders"
        assert str(ref) == "db.orders"

    def test_unqualified_full_name(self):
        assert TableRef("", "orders").full_name == "orders"

    def test_parse(self):
        assert TableRef.parse("db.orders") == TableRef("db", "orders")

    def test_parse_keeps_dots_after_first(self):
        assert TableRef.parse("db.schema.t") == TableRef("db", "schema.t")

    @pytest.mark.parametrize("text", ["orders", "db.", ".orders", ""])
    def test_parse_rejects_bad_format(self, text):
        with pytest.raises(SchemaError):
            TableRef.parse(text)

    def test_is_view(self):
        assert TableRef("", "mv_daily").is_view
        assert not TableRef("db", "orders").is_view

    def test_hashable_and_equal(self):
        assert {TableRef("a", "b"), TableRef("a", "b")} == {TableRef("a", "b")}
        assert TableRef("a", "b") != TableRef("a", "c")

    def test_coerce_ref(self):
        assert coerce_ref("a.b") == TableRef("a", "b")
        assert coerce_ref(TableRef("a", "b")) == TableRef("a", "b")


class TestTableSchema:
    def test_column_names_and_dict(self):
        schema = TableSchema([ColumnInfo("id", "integer", False), ColumnInfo("n", "varchar")], 3)
        assert schema.column_names == {"id", "n"}
        assert schema.as_dict() == {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "n", "type": "varchar", "nullable": True},
            ],
            "row_count": 3,
        }

    def test_defaults(self):
        assert TableSchema().row_count == -1
        assert TableSchema().columns == []


class TestRowSet:
    def test_from_records_first_seen_column_order_and_missing_keys(self):
        rs = RowSet.from_records([{"a": 1, "b": 2}, {"b": 3, "c": 4}])
        assert rs.columns == ("a", "b", "c")
        assert rs.rows == [(1, 2, None), (None, 3, 4)]

    def test_from_records_empty(self):
        rs = RowSet.from_records([])
        assert rs.is_empty
        assert rs.columns == ()
        assert len(rs) == 0

    def test_to_records_roundtrip(self):
        records = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        assert RowSet.from_records(records).to_records() == records

    def test_column_and_head(self):
        rs = RowSet(("a", "b"), [(1, 2), (3, 4), (5, 6)])
        assert rs.column("b") == [2, 4, 6]
        assert rs.head(2).rows == [(1, 2), (3, 4)]
        with pytest.raises(ValueError):
            rs.column("zzz")


class TestQueryResult:
    def setup_method(self):
        self.result = QueryResult(
            columns=["id", "name", "amount"],
            rows=[(1, "Alice", 100.0), (2, "Bob", 50.0), (3, "Charlie", 200.0)],
            sql="SELECT * FROM users",
            execution_time_ms=5.5,
        )

    def test_counts(self):
        assert self.result.row_count == 3
        assert self.result.column_count == 3
        assert len(self.result) == 3

    def test_repr(self):
        assert "rows=3" in repr(self.result)
        assert "cols=3" in repr(self.result)

    def test_to_records(self):
        recs = self.result.to_records()
        assert recs[0]["name"] == "Alice"
        assert recs[1]["amount"] == 50.0

    def test_to_json(self):
        parsed = json.loads(self.result.to_json())
        assert len(parsed) == 3
        assert parsed[0]["name"] == "Alice"

    def test_to_csv(self):
        text = self.result.to_csv()
        assert text.splitlines()[0] == "id,name,amount"
        assert "Alice" in text

    def test_to_markdown_structure(self):
        lines = self.result.to_markdown().splitlines()
        assert lines[0].startswith("| id")
        assert set(lines[1]) <= {"|", "-"}
        assert len(lines) == 2 + 3
        assert "Alice" in lines[2]

    def test_to_markdown_none_renders_empty(self):
        md = QueryResult(["a"], [(None,)]).to_markdown()
        assert "None" not in md

    def test_summary(self):
        s = self.result.summary()
        assert "3 rows" in s
        assert "Alice" in s

    def test_summary_truncates_after_five(self):
        r = QueryResult(["a"], [(i,) for i in range(8)])
        assert "... and 3 more rows" in r.summary()

    def test_rowset_roundtrip_and_as_cached(self):
        rs = self.result.to_rowset()
        again = QueryResult.from_rowset(rs, sql="q", execution_time_ms=1.0)
        assert again.rows == self.result.rows
        cached = again.as_cached()
        assert cached.from_cache is True
        assert cached.execution_time_ms == 0.0
        assert again.from_cache is False  # original untouched

    def test_empty(self):
        r = QueryResult(columns=["a"], rows=[])
        assert len(r) == 0
        assert r.to_records() == []
        assert r.to_csv() == "a\n"


class TestFetchPlan:
    def test_add_deduplicates(self):
        plan = FetchPlan()
        plan.add(TableRef("s", "t"))
        plan.add(TableRef("s", "t"))
        assert len(plan.targets) == 1

    def test_is_empty(self):
        assert FetchPlan().is_empty()
        plan = FetchPlan()
        plan.add(TableRef("s", "t"))
        assert not plan.is_empty()

    def test_pushdown_eligibility(self):
        plan = FetchPlan(
            targets=[TableRef("s", "t")],
            is_single_source=True,
            source_name="s",
            all_targets_unloaded=True,
        )
        assert plan.pushdown_eligible
        plan.has_mv_reference = True
        assert not plan.pushdown_eligible
        plan.has_mv_reference = False
        plan.all_targets_unloaded = False
        assert not plan.pushdown_eligible
        assert not FetchPlan().pushdown_eligible


class TestBackupInfo:
    def test_as_dict(self):
        info = BackupInfo(
            name="fusion_backup_x",
            path=Path("/tmp/fusion_backup_x"),
            kind="export",
            size_bytes=2 * 1024 * 1024,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        d = info.as_dict()
        assert d["kind"] == "export"
        assert d["size_mb"] == 2.0
        assert d["created_at"].startswith("2026-01-02")
