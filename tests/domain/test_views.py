"""Tests for materialized view rules."""

import pytest

from fusion.domain.views import PRIORITY_ORDER, ViewSpec, parse_refresh_interval


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("manual", 0),
        ("hourly", 3600),
        ("daily", 86400),
        ("every 15 minutes", 900),
        ("every 1 hour", 3600),
        ("Every 2 Days", 172800),
        ("every 30 seconds", 30),
        ("whenever", 0),
    ],
)
def test_parse_refresh_interval(text, seconds):
    assert parse_refresh_interval(text) == seconds


def test_view_spec_table_name_and_rank():
    spec = ViewSpec(name="daily", sql="SELECT 1", priority="high")
    assert spec.table_name == "mv_daily"
    assert spec.priority_rank == PRIORITY_ORDER["high"]
    assert ViewSpec(name="x", sql="", priority="bogus").priority_rank == PRIORITY_ORDER["normal"]
    assert spec.as_dict()["table_name"] == "mv_daily"
