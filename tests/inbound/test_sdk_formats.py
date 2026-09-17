"""Tests for the optional pandas / Arrow SDK conversions."""

import pytest

from fusion.adapters.inbound.sdk.formats import rowset_from_dataframe, to_arrow, to_dataframe
from fusion.domain.models import QueryResult, RowSet

pd = pytest.importorskip("pandas")

RESULT = QueryResult(columns=["id", "name"], rows=[(1, "a"), (2, None)])


def test_to_dataframe():
    df = to_dataframe(RESULT)
    assert list(df.columns) == ["id", "name"]
    assert len(df) == 2
    assert df.iloc[0]["name"] == "a"


def test_rowset_from_dataframe_plain_python_values():
    df = pd.DataFrame({"id": [1, 2], "score": [1.5, float("nan")], "name": ["a", None]})
    rs = rowset_from_dataframe(df)
    assert rs.columns == ("id", "score", "name")
    assert rs.rows[0] == (1, 1.5, "a")
    assert rs.rows[1] == (2, None, None)
    assert type(rs.rows[0][0]) is int


def test_roundtrip():
    rs = RowSet(("a", "b"), [(1, "x"), (2, "y")])
    assert rowset_from_dataframe(to_dataframe(rs)).rows == rs.rows


def test_to_arrow():
    table = to_arrow(RESULT)
    assert table.column_names == ["id", "name"]
    assert table.num_rows == 2
