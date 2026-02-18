"""Tests for QueryResult."""

import json
import os
import tempfile

from fusion.result import QueryResult


class TestQueryResult:
    def setup_method(self):
        self.result = QueryResult(
            data=[(1, "Alice", 100.0), (2, "Bob", 50.0), (3, "Charlie", 200.0)],
            columns=["id", "name", "amount"],
            sql="SELECT * FROM users",
            execution_time=5.5,
            from_cache=False,
        )

    def test_row_count(self):
        assert self.result.row_count == 3

    def test_column_count(self):
        assert self.result.column_count == 3

    def test_columns(self):
        assert self.result.columns == ["id", "name", "amount"]

    def test_len(self):
        assert len(self.result) == 3

    def test_repr(self):
        r = repr(self.result)
        assert "rows=3" in r
        assert "cols=3" in r

    def test_to_dataframe(self):
        df = self.result.to_dataframe()
        assert len(df) == 3
        assert list(df.columns) == ["id", "name", "amount"]

    def test_to_dict(self):
        d = self.result.to_dict()
        assert len(d) == 3
        assert d[0]["name"] == "Alice"
        assert d[1]["amount"] == 50.0

    def test_to_markdown(self):
        md = self.result.to_markdown()
        assert "Alice" in md
        assert "id" in md

    def test_to_json(self):
        j = self.result.to_json()
        parsed = json.loads(j)
        assert len(parsed) == 3
        assert parsed[0]["name"] == "Alice"

    def test_to_csv_string(self):
        csv = self.result.to_csv()
        assert "Alice" in csv
        assert "id,name,amount" in csv

    def test_to_csv_file(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.result.to_csv(path)
            with open(path) as f:
                content = f.read()
            assert "Alice" in content
        finally:
            os.unlink(path)

    def test_summary(self):
        s = self.result.summary()
        assert "3 rows" in s
        assert "Alice" in s

    def test_empty_result(self):
        r = QueryResult(data=[], columns=["a"], sql="", execution_time=0)
        assert len(r) == 0
        assert r.to_dict() == []
        df = r.to_dataframe()
        assert len(df) == 0
