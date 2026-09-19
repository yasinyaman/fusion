"""One-edit repair: the metric a copilot is actually judged by.

Execution accuracy asks whether an arm was right on its own. These tests cover
the other question — when it was wrong, was it *nearly* right — and the
property that makes that question answerable at all.
"""

import pytest

from bench.constrain import request_schema, to_dsl
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    as_records,
)
from bench.fusion_source import fixture_factory
from bench.repair import (
    MAX_NEIGHBOURS,
    branch_for,
    edit_distance_label,
    neighbours,
    one_edit_away,
    summarise,
)
from bench.scoring import rows_equal

TABLES = ["banka.islemler", "banka.musteriler"]


@pytest.fixture
def fusion():
    from fusion import Settings, build_app

    app = build_app(
        Settings(),
        source_factory=fixture_factory(
            {
                "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
                "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
            }
        ),
    )
    app.sources.connect("banka", {"type": "fixture"})
    yield app
    app.close()


@pytest.fixture
def schema(fusion):
    return request_schema(fusion.semantic, TABLES)


@pytest.fixture
def run(fusion):
    def _run(request):
        normalised = to_dsl(request)
        table = normalised["table"] if normalised["table"] in TABLES else TABLES[0]
        result = fusion.tools.execute(
            "query_metrics",
            {
                "table": table,
                "metrics": normalised["metrics"],
                "dimensions": normalised["dimensions"],
                "filters": normalised["filters"],
                "order_by": normalised["order_by"],
                "limit": normalised["limit"],
            },
        )
        if "error" in result:
            raise RuntimeError(result["error"])
        columns = result["columns"]
        return [tuple(row[name] for name in columns) for row in result["rows"]]

    return _run


# -- the neighbourhood -----------------------------------------------------


def test_every_neighbour_is_a_request_the_layer_accepts(schema, run):
    """An edit may never produce something the layer would refuse.

    This is what makes a repair a real offer: anything a copilot could put in
    front of a person has already been shown to run.
    """
    checked = 0
    for table in TABLES:
        for dimensions in (
            [],
            [{"column": "kanal"}],
            [{"column": "islem_tarihi", "grain": "month"}],
        ):
            start = {
                "table": table,
                "metrics": [{"measure": "*", "aggregation": "count"}],
                "dimensions": [d for d in dimensions if table.endswith("islemler")],
                "filters": [],
            }
            for candidate in neighbours(start, branch_for(schema, table)):
                run(candidate)
                checked += 1
    assert checked > 200, f"only {checked} neighbours were exercised"


def test_the_neighbourhood_stays_small_enough_to_search(schema):
    start = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [{"column": "kanal"}],
        "filters": [],
    }
    found = list(neighbours(start, branch_for(schema, "banka.islemler")))
    assert 20 < len(found) < MAX_NEIGHBOURS
    # Every one differs from where it started.
    assert all(candidate != start for candidate in found)


def test_an_edit_changes_exactly_one_thing(schema):
    start = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [{"column": "kanal"}],
        "filters": [{"column": "durum", "op": "eq", "value": "basarili"}],
    }
    for candidate in neighbours(start, branch_for(schema, "banka.islemler")):
        differing = [
            field
            for field in ("metrics", "dimensions", "filters", "descending")
            if candidate.get(field) != start.get(field)
        ]
        assert len(differing) == 1, candidate


# -- the repair ------------------------------------------------------------


def test_a_wrong_aggregation_is_one_edit_away(fusion, schema, run):
    """`tutar:avg` where the question wanted `tutar:sum`: one word."""
    wrong = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "avg"}],
        "dimensions": [],
        "filters": [],
    }
    gold = run({"table": "banka.islemler", "metrics": [{"measure": "tutar", "aggregation": "sum"}]})
    repaired = one_edit_away(
        wrong, "banka.islemler", schema, run, lambda rows: rows_equal(rows, gold)
    )
    assert repaired is not None
    assert repaired["metrics"][0]["aggregation"] == "sum"


def test_a_missing_condition_is_one_edit_away(fusion, schema, run):
    """The commonest failure: the question said "başarılı" and it was dropped."""
    wrong = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [],
        "filters": [],
    }
    gold = run(dict(wrong, filters=[{"column": "durum", "op": "eq", "value": "basarili"}]))
    repaired = one_edit_away(
        wrong, "banka.islemler", schema, run, lambda rows: rows_equal(rows, gold)
    )
    assert repaired is not None
    assert repaired["filters"] == [{"column": "durum", "op": "eq", "value": "basarili"}]


def test_a_missing_breakdown_is_one_edit_away(fusion, schema, run):
    wrong = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [],
        "filters": [],
    }
    gold = run(dict(wrong, dimensions=[{"column": "kanal"}]))
    repaired = one_edit_away(
        wrong, "banka.islemler", schema, run, lambda rows: rows_equal(rows, gold)
    )
    assert repaired is not None
    assert repaired["dimensions"] == [{"column": "kanal"}]


def test_two_mistakes_are_not_one_edit_away(fusion, schema, run):
    """The metric has to be able to say no, or it says nothing."""
    wrong = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "avg"}],
        "dimensions": [],
        "filters": [],
    }
    gold = run(
        {
            "table": "banka.islemler",
            "metrics": [{"measure": "tutar", "aggregation": "sum"}],
            "dimensions": [{"column": "kanal"}],
            "filters": [{"column": "durum", "op": "eq", "value": "basarili"}],
        }
    )
    assert (
        one_edit_away(wrong, "banka.islemler", schema, run, lambda rows: rows_equal(rows, gold))
        is None
    )


def test_a_failing_neighbour_does_not_abort_the_search(fusion, schema, run):
    """Some edits raise; the search has to walk past them to the one that works."""
    wrong = {
        "table": "banka.musteriler",
        "metrics": [{"measure": "musteri_id", "aggregation": "sum"}],
        "dimensions": [],
        "filters": [],
    }
    gold = run({"table": "banka.musteriler", "metrics": [{"measure": "*", "aggregation": "count"}]})
    repaired = one_edit_away(
        wrong, "banka.musteriler", schema, run, lambda rows: rows_equal(rows, gold)
    )
    assert repaired is not None


# -- reporting -------------------------------------------------------------


def test_the_three_buckets_say_what_a_person_has_to_do():
    assert edit_distance_label(True, None) == "correct"
    assert edit_distance_label(False, {"metrics": []}) == "one edit away"
    assert edit_distance_label(False, None) == "needs rethinking"


def test_a_summary_keeps_every_bucket_even_when_empty():
    counted = summarise(["correct", "correct", "needs rethinking"])
    assert counted == {"correct": 2, "one edit away": 0, "needs rethinking": 1}
    assert list(counted) == ["correct", "one edit away", "needs rethinking"]


def test_branch_lookup_falls_back_rather_than_raising(schema):
    assert branch_for(schema, "banka.islemler")["properties"]["table"]["const"] == "banka.islemler"
    # An unknown table gets the first branch, so a caller never has to guard.
    assert branch_for(schema, "banka.yok")["properties"]["table"]["const"]


# -- the edits really are single --------------------------------------------


def test_a_breakdown_is_never_replaced_wholesale(schema):
    """Two dimensions dropped and one added is not one edit.

    `test_an_edit_changes_exactly_one_thing` compares top-level field keys, so
    it passed while `dimensions=[one]` replaced a list of two — which counted
    a two-field fix as a near miss and overstated the repair rate.
    """
    start = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [{"column": "kanal"}, {"column": "durum"}],
        "filters": [],
    }
    for candidate in neighbours(start, branch_for(schema, "banka.islemler")):
        chosen = candidate["dimensions"]
        if chosen == start["dimensions"]:
            continue
        added = [d for d in chosen if d not in start["dimensions"]]
        removed = [d for d in start["dimensions"] if d not in chosen]
        assert len(added) + len(removed) == 1, candidate


def test_one_dimension_may_still_be_swapped(schema):
    """With a single breakdown, replacing it *is* one edit."""
    start = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [{"column": "kanal"}],
        "filters": [],
    }
    found = [
        c["dimensions"]
        for c in neighbours(start, branch_for(schema, "banka.islemler"))
        if c["dimensions"] == [{"column": "durum"}]
    ]
    assert found, "a lone dimension should be swappable"


def test_string_metrics_do_not_crash_the_search(schema, run):
    """The unconstrained arm answers in DSL strings, and `to_dsl` accepts them.

    Reaching for `.get` on a `str` used to raise out of the generator and past
    `one_edit_away`'s guard, so the copilot metric was uncomputable for exactly
    the arm whose failures are most interesting.
    """
    start = {
        "table": "banka.islemler",
        "metrics": ["tutar:sum"],
        "dimensions": ["kanal"],
        "filters": [],
    }
    found = list(neighbours(start, branch_for(schema, "banka.islemler")))

    assert found, "dimension and filter edits still apply"
    # Metric edits need the structured form, so none are offered.
    assert all(c["metrics"] == ["tutar:sum"] for c in found)


def test_a_missing_condition_is_offered_before_the_cap(schema):
    """The cap used to fall entirely on filter edits.

    Metric swaps were emitted first, so on a wide table the search gave up
    before offering a single condition — the commonest real repair.
    """
    start = {
        "table": "banka.islemler",
        "metrics": [{"measure": "tutar", "aggregation": "sum"}],
        "dimensions": [],
        "filters": [],
    }
    early = []
    for index, candidate in enumerate(neighbours(start, branch_for(schema, "banka.islemler"))):
        if index >= 12:
            break
        early.append(candidate)

    assert any(c["filters"] for c in early), "no condition offered in the first dozen edits"
    assert any(c["dimensions"] for c in early), "no breakdown offered either"
