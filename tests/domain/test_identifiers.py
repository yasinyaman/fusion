"""Tests for identifier rules."""

import pytest

from fusion.domain.errors import QueryError
from fusion.domain.identifiers import (
    ALLOWED_AGG_FUNCS,
    MAX_RESULT_ROWS,
    is_valid_view_name,
    validate_identifier,
)


@pytest.mark.parametrize("name", ["orders", "db.orders", "_x1", "A.b_c"])
def test_valid_identifiers(name):
    validate_identifier(name)


@pytest.mark.parametrize("name", ["", "1abc", "a b", "a;b", "a'b", "a-b", "a/*b*/", None])
def test_invalid_identifiers(name):
    with pytest.raises(QueryError):
        validate_identifier(name, "table name")


def test_view_names_have_no_dots():
    assert is_valid_view_name("daily_revenue")
    assert not is_valid_view_name("db.daily")
    assert not is_valid_view_name("1x")


def test_constants():
    assert MAX_RESULT_ROWS == 100
    assert ALLOWED_AGG_FUNCS == {"SUM", "AVG", "COUNT", "MIN", "MAX"}
