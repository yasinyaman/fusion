"""End-to-end test of the harness, with scripted models and a fake database.

The harness has to be trustworthy before its numbers mean anything, and that
cannot be established with a real model in the loop. Every arm here is driven
by fixed replies, so the expected report is known exactly.
"""

import json

import pytest

from bench.arms import ScriptedModel, TextToSqlArm
from bench.dataset import Question, QuestionSet, load_question_set, paired_ids
from bench.models import extract_json
from bench.runner import ArmResult, build_report, compare, run_arm, verdict, write_report
from bench.scoring import Grade

QUESTIONS = (
    Question(id="q1", text="total?", gold={"default": "SELECT SUM(a) FROM t"}),
    Question(id="q2", text="count?", gold={"default": "SELECT COUNT(*) FROM t"}),
)
GOLD = {"q1": [(100.0,)], "q2": [(7,)]}


class FakeExecutor:
    """Returns a canned result per statement; anything else raises."""

    def __init__(self, results: dict[str, list[tuple]]) -> None:
        self.results = results
        self.ran: list[str] = []

    def run(self, sql: str):
        self.ran.append(sql)
        if sql not in self.results:
            raise RuntimeError(f"no such column in: {sql}")
        return self.results[sql]


def arm(name: str, replies: dict[str, str], results: dict[str, list[tuple]]) -> TextToSqlArm:
    return TextToSqlArm(
        name=name,
        model=ScriptedModel(replies),
        executor=FakeExecutor(results),
        context="CREATE TABLE t (a NUMBER);",
    )


class TestRunArm:
    def test_a_correct_arm_scores_every_question(self):
        right = arm(
            "right",
            {"total?": "SELECT SUM(a) FROM t", "count?": "SELECT COUNT(*) FROM t"},
            {"SELECT SUM(a) FROM t": [(100.0,)], "SELECT COUNT(*) FROM t": [(7,)]},
        )
        result = run_arm(right, QUESTIONS, GOLD, "default", ["t", "a"])
        assert [g.correct for g in result.grades.values()] == [True, True]
        assert result.accuracy(["q1", "q2"]).point == 1.0

    def test_an_arm_that_invents_a_column_is_wrong_and_diagnosable(self):
        wrong = arm(
            "wrong",
            {"total?": "SELECT SUM(nope) FROM t", "count?": "SELECT COUNT(*) FROM t"},
            {"SELECT COUNT(*) FROM t": [(7,)]},
        )
        result = run_arm(wrong, QUESTIONS, GOLD, "default", ["t", "a"])
        assert not result.grades["q1"].correct
        assert result.grades["q1"].hallucinated == ("nope",)
        assert "no such column" in result.grades["q1"].error
        assert result.hallucination_rate(["q1", "q2"]) == 0.5

    def test_an_arm_that_raises_does_not_abort_the_run(self):
        class Exploding:
            name = "exploding"

            def answer(self, question):
                raise RuntimeError("boom")

        result = run_arm(Exploding(), QUESTIONS, GOLD, "default", [])
        assert len(result.grades) == 2
        assert all("boom" in g.error for g in result.grades.values())

    def test_markdown_fenced_sql_is_accepted(self):
        fenced = arm(
            "fenced",
            {"total?": "```sql\nSELECT SUM(a) FROM t\n```", "count?": "SELECT COUNT(*) FROM t"},
            {"SELECT SUM(a) FROM t": [(100.0,)], "SELECT COUNT(*) FROM t": [(7,)]},
        )
        result = run_arm(fenced, QUESTIONS, GOLD, "default", ["t", "a"])
        assert result.grades["q1"].correct


class TestReport:
    def _results(self) -> list[ArmResult]:
        raw = ArmResult("raw", {"q1": Grade(correct=False), "q2": Grade(correct=True)})
        catalog = ArmResult("catalog", {"q1": Grade(correct=True), "q2": Grade(correct=True)})
        semantic = ArmResult("semantic", {"q1": Grade(correct=True), "q2": Grade(correct=True)})
        return [raw, catalog, semantic]

    def test_it_compares_the_last_arm_against_the_others(self):
        report = build_report(QuestionSet("demo", QUESTIONS), "oracle", self._results())
        assert [(c.first, c.second) for c in report.comparisons] == [
            ("semantic", "raw"),
            ("semantic", "catalog"),
        ]

    def test_a_question_an_arm_skipped_is_dropped_and_named(self):
        # McNemar needs the same questions on both sides; shrinking the set
        # silently would make the pairing a lie.
        results = self._results()
        del results[0].grades["q2"]
        report = build_report(QuestionSet("demo", QUESTIONS), "oracle", results)
        assert report.ids == ["q1"]
        assert report.dropped == ["q2"]

    def test_markdown_carries_the_numbers(self):
        report = build_report(QuestionSet("demo", QUESTIONS), "oracle", self._results())
        text = report.to_markdown()
        assert "demo on oracle" in text
        assert "| semantic |" in text
        assert "McNemar" in text
        assert "diagnostic" in text  # the exact-match caveat

    def test_it_writes_both_formats(self, tmp_path):
        report = build_report(QuestionSet("demo", QUESTIONS), "oracle", self._results())
        json_path, md_path = write_report(report, tmp_path)
        assert json.loads(json_path.read_text())["dialect"] == "oracle"
        assert md_path.read_text().startswith("# Benchmark")


class TestVerdict:
    def _comparison(self, gap: float):
        first = [True] * int(gap) + [False] * (100 - int(gap))
        second = [False] * 100
        return compare(
            ArmResult("semantic", {str(i): Grade(correct=v) for i, v in enumerate(first)}),
            ArmResult("other", {str(i): Grade(correct=v) for i, v in enumerate(second)}),
            [str(i) for i in range(100)],
        )

    def test_a_small_gain_over_the_catalog_says_stop(self):
        # The plan's threshold, applied mechanically — which is the point of
        # having fixed it before the run.
        text = verdict(self._comparison(3), self._comparison(40))
        assert "Stop investing" in text

    def test_a_large_gain_over_raw_says_fund_it(self):
        text = verdict(self._comparison(20), self._comparison(40))
        assert "Fund the DSL" in text

    def test_in_between_says_ship_p0_and_defer_joins(self):
        text = verdict(self._comparison(10), self._comparison(12))
        assert "defer joins" in text


class TestDataset:
    def test_the_shipped_question_set_loads(self):
        questions = load_question_set("bench/questions/banka-tr-en.json")
        assert len(questions) == 40
        assert len(questions.by_language("tr")) == 20
        assert len(questions.by_language("en")) == 20

    def test_every_question_has_gold_for_both_dialects(self):
        questions = load_question_set("bench/questions/banka-tr-en.json")
        for question in questions:
            assert question.gold_for("oracle"), question.id
            assert question.gold_for("mssql"), question.id

    def test_the_dialects_genuinely_differ_somewhere(self):
        # Otherwise the per-dialect gold is pointless ceremony.
        questions = load_question_set("bench/questions/banka-tr-en.json")
        differing = [q for q in questions if q.gold_for("oracle") != q.gold_for("mssql")]
        assert len(differing) >= 4

    def test_duplicate_ids_are_refused(self, tmp_path):
        path = tmp_path / "dupes.json"
        path.write_text(
            json.dumps({"questions": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]})
        )
        with pytest.raises(ValueError, match="Duplicate question ids"):
            load_question_set(path)

    def test_paired_ids_keeps_only_what_every_arm_answered(self):
        assert paired_ids([["a", "b", "c"], ["a", "c"], ["c", "a"]]) == ["a", "c"]


class TestSubsetReport:
    """Separating "got it wrong" from "was never able to answer that kind"."""

    def _report(self):

        raw = ArmResult("raw", {"q1": Grade(correct=True), "q2": Grade(correct=True)})
        semantic = ArmResult("semantic", {"q1": Grade(correct=True), "q2": Grade(correct=False)})
        return build_report(QuestionSet("demo", QUESTIONS), "duckdb", [raw, semantic])

    def test_it_restricts_to_the_named_questions(self):
        from bench.runner import subset_report

        subset = subset_report(self._report(), ["q1"], "single-table")
        assert subset.ids == ["q1"]
        assert "single-table" in subset.question_set

    def test_the_comparison_is_recomputed_on_the_subset(self):
        from bench.runner import subset_report

        full = self._report()
        assert full.comparisons[0].gap_points == -50.0  # semantic lost q2
        subset = subset_report(full, ["q1"], "single-table")
        assert subset.comparisons[0].gap_points == 0.0  # tied on q1

    def test_an_id_outside_the_run_is_ignored(self):
        from bench.runner import subset_report

        assert subset_report(self._report(), ["q1", "nope"], "x").ids == ["q1"]


class TestJsonExtraction:
    """Models wrap answers in prose; refusing those measures formatting, not skill."""

    def test_a_bare_object(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_a_fenced_block(self):
        assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_an_object_buried_in_prose(self):
        reply = 'To find the total, use:\n\n```json\n{"a": 1}\n```\n\nThis sums the column.'
        assert extract_json(reply) == '{"a": 1}'

    def test_nested_objects_are_balanced(self):
        payload = '{"a": {"b": 2}, "c": 3}'
        assert extract_json(f"here: {payload} done") == payload

    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        payload = '{"a": "}{", "b": 1}'
        assert extract_json(payload) == payload

    def test_an_escaped_quote_inside_a_string(self):
        payload = '{"a": "say \\"hi\\"", "b": 1}'
        assert extract_json(payload) == payload

    def test_no_object_at_all_returns_the_text(self):
        assert extract_json("I cannot answer that") == "I cannot answer that"


class TestCliLoader:
    """`--model` and `--executor` may name an object or a factory for one."""

    def test_an_instance_is_returned_as_is(self):
        from bench.__main__ import _load

        assert _load("bench.arms:ScriptedModel", "__call__") is not None

    def test_a_factory_is_called(self):
        # A database connection usually has to be opened at call time, so the
        # attribute is often a factory rather than the object itself.
        from bench.__main__ import _load
        from bench.models import OllamaModel

        assert isinstance(_load("bench.models:ollama_remote", "complete"), OllamaModel)

    def test_a_missing_attribute_says_so(self):
        from bench.__main__ import _load

        with pytest.raises(SystemExit, match="Could not load"):
            _load("bench.models:not_there", "complete")

    def test_a_malformed_path_says_the_shape(self):
        from bench.__main__ import _load

        with pytest.raises(SystemExit, match="module:attribute"):
            _load("bench.models", "complete")

    def test_something_of_the_wrong_kind_is_refused(self):
        from bench.__main__ import _load

        with pytest.raises(SystemExit, match="has no .run"):
            _load("bench.models:OllamaModel", "run")


class TestNamedExtraction:
    """What a semantic request actually referenced from the schema."""

    def test_measures_dimensions_filters_and_the_table(self):
        from bench.arms import _named_in

        named = _named_in(
            {
                "metrics": ["tutar:sum", "*:count"],
                "dimensions": ["kanal", "islem_tarihi:month"],
                "filters": [{"column": "durum", "value": "basarili"}],
            },
            "banka.islemler",
        )
        assert named == ("durum", "islem_tarihi", "islemler", "kanal", "tutar")

    def test_transforms_are_the_language_not_the_schema(self):
        from bench.arms import _named_in

        assert _named_in({"metrics": ["change_pct(cumsum(tutar:sum))"]}, "db.t") == ("t", "tutar")

    def test_the_row_measure_names_no_column(self):
        from bench.arms import _named_in

        assert _named_in({"metrics": ["*:count"]}, "db.t") == ("t",)

    def test_an_empty_request(self):
        from bench.arms import _named_in

        assert _named_in({}, "db.t") == ("t",)
