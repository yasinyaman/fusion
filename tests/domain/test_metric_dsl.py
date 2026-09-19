"""Tests for the metric expression parser."""

import pytest

from fusion.domain.errors import QueryError
from fusion.domain.identifiers import IDENTIFIER_RE
from fusion.domain.metric_dsl import (
    MAX_NESTING,
    MeasureRef,
    TransformCall,
    depth,
    measure_ref_of,
    needs_order,
    needs_time,
    parse_metric,
    transforms_of,
)

VALID = [
    "revenue:sum",
    "*:count",
    "*:count_distinct",
    "customer_id:count_distinct",
    "price:weighted_avg(weight=quantity)",
    "cumsum(revenue:sum)",
    "change_pct(cumsum(revenue:sum))",
    "lag(revenue:sum, n=2)",
    "lead(revenue:sum)",
    "ntile(revenue:sum, buckets=4)",
    "time_shift(revenue:sum, periods=1)",
    "rank(amount:max)",
    "percent_rank(amount:avg)",
    "dense_rank(amount:min)",
    "change(cumsum(lag(revenue:sum, n=3)))",
]


@pytest.mark.parametrize("text", VALID)
def test_round_trip(text):
    # as_text() renders the canonical form, which must parse back to the same tree.
    parsed = parse_metric(text)
    assert parse_metric(parsed.as_text()) == parsed


@pytest.mark.parametrize("text", VALID)
def test_output_names_are_bare_identifiers(text):
    # The output name becomes a quoted SQL alias and a result column, so it
    # must never need escaping.
    name = parse_metric(text).output_name()
    assert IDENTIFIER_RE.match(name)
    assert "." not in name


def test_whitespace_and_case_are_normalized():
    assert parse_metric("  revenue:SUM  ") == MeasureRef(measure="revenue", agg="SUM")
    assert parse_metric("lag( revenue:sum , n = 2 )") == parse_metric("lag(revenue:sum,n=2)")


def test_the_tree_shape():
    parsed = parse_metric("change_pct(cumsum(revenue:sum))")
    assert isinstance(parsed, TransformCall) and parsed.name == "change_pct"
    assert isinstance(parsed.inner, TransformCall) and parsed.inner.name == "cumsum"
    assert measure_ref_of(parsed) == MeasureRef(measure="revenue", agg="SUM")
    assert [call.name for call in transforms_of(parsed)] == ["cumsum", "change_pct"]
    assert depth(parsed) == 2
    assert parsed.output_name() == "change_pct_cumsum_revenue_sum"


def test_row_measure_naming():
    assert parse_metric("*:count").output_name() == "count"
    assert parse_metric("*:count_distinct").output_name() == "count_distinct"


class TestNeeds:
    def test_only_time_shift_needs_a_calendar(self):
        assert needs_time(parse_metric("time_shift(revenue:sum)"))
        assert needs_time(parse_metric("change(time_shift(revenue:sum))"))
        assert not needs_time(parse_metric("lag(revenue:sum)"))
        assert not needs_time(parse_metric("revenue:sum"))

    def test_a_bare_measure_needs_no_ordering(self):
        assert not needs_order(parse_metric("revenue:sum"))
        assert needs_order(parse_metric("rank(revenue:sum)"))


class TestNesting:
    def test_the_limit_is_enforced(self):
        deep = "revenue:sum"
        for _ in range(MAX_NESTING + 1):
            deep = f"change({deep})"
        with pytest.raises(QueryError, match=f"limited to {MAX_NESTING} levels"):
            parse_metric(deep)

    def test_exactly_the_limit_is_allowed(self):
        at_limit = "revenue:sum"
        for _ in range(MAX_NESTING):
            at_limit = f"change({at_limit})"
        assert depth(parse_metric(at_limit)) == MAX_NESTING


# (expression, fragment that must appear in the message)
MALFORMED = [
    ("", "cannot be empty"),
    ("   ", "cannot be empty"),
    ("revenue", "expected ':' and an aggregation"),
    ("revenue:", "expected an aggregation after ':'"),
    ("revenue:summ", "Did you mean 'sum'?"),
    ("cumsun(revenue:sum)", "Did you mean 'cumsum'?"),
    ("change_pct(cumsum(revenue:sum)", "expected"),
    ("cumsum()", "expected a measure name"),
    ("cumsum(revenue:sum))", "unexpected ')' after the metric"),
    ("ntile(revenue:sum)", "needs a 'buckets' argument"),
    ("lag(revenue:sum, n=0)", "at least 1"),
    ("lag(revenue:sum, n=x)", "whole number"),
    ("lag(revenue:sum, k=2)", "has no argument 'k'"),
    ("cumsum(revenue:sum, n=2)", "Accepted arguments: none"),
    ("revenue:sum extra", "unexpected 'extra' after the metric"),
    ("*:sum", "only takes count"),
    ("price:weighted_avg()", "needs a 'weight' argument"),
    ("price:weighted_avg(weight=2)", "must name a column"),
    ("revenue:sum(weight=q)", "Accepted arguments: none"),
    ("lag(revenue:sum, =2)", "expected an argument name"),
    ("lag(revenue:sum, n)", "expected '='"),
    (":sum", "expected a measure name"),
    ("1revenue:sum", "expected a measure name"),
]


@pytest.mark.parametrize(("text", "fragment"), MALFORMED)
def test_malformed_expressions_say_what_is_wrong(text, fragment):
    with pytest.raises(QueryError) as excinfo:
        parse_metric(text)
    assert fragment in str(excinfo.value)


# Characters the DSL alphabet does not contain. Rejecting them at the character
# means an expression can never carry a quote escape or a statement separator.
@pytest.mark.parametrize(
    "text",
    [
        "revenue:sum; DROP TABLE t",
        "revenue:sum--",
        "revenue:sum/*x*/",
        'revenue:sum"',
        "revenue:sum\\",
        "read_csv:sum|x",
    ],
)
def test_characters_outside_the_alphabet_are_refused(text):
    with pytest.raises(QueryError, match="unexpected character"):
        parse_metric(text)


def test_an_unterminated_quote_is_refused():
    with pytest.raises(QueryError, match="unterminated quoted value"):
        parse_metric("rev'enue:sum")


def test_the_caret_points_at_the_problem():
    with pytest.raises(QueryError) as excinfo:
        parse_metric("revenue:sum extra")
    lines = str(excinfo.value).splitlines()
    # The message, then the expression, then a caret under position 12.
    assert lines[1].strip() == "revenue:sum extra"
    assert lines[2].index("^") - 2 == 12


@pytest.mark.parametrize("text", [None, 42, []])
def test_non_string_input_is_refused(text):
    with pytest.raises(QueryError, match="cannot be empty"):
        parse_metric(text)
