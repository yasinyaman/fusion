"""The figures the README quotes, recomputed from the checked-in results.

A number in a write-up drifts from the data behind it the first time either is
edited. These fail when that happens.
"""

import json
from pathlib import Path

import pytest

from bench.scoring import hallucinated_identifiers, rows_equal
from bench.stats import sign_test_by_cluster

BENCH = Path(__file__).resolve().parents[1]
JOIN_QUESTIONS = {
    q["id"]
    for q in json.loads((BENCH / "questions" / "banka-tr-en.json").read_text())["questions"]
    if "join" in q["tags"]
}


def _grades(model: str) -> dict[str, dict[str, dict]]:
    path = BENCH / f"results-{model}" / "banka-tr-en-duckdb.json"
    return json.loads(path.read_text())["grades"]


def _question(question_id: str) -> str:
    """``q07-tr`` and ``q07-en`` are one question asked twice."""
    return question_id.rsplit("-", 1)[0]


def _by_question(model: str, first: str, second: str, single_table: bool = False):
    grades = _grades(model)
    keep = [q for q in grades[first] if not (single_table and q in JOIN_QUESTIONS)]
    return sign_test_by_cluster(
        {q: grades[first][q]["correct"] for q in keep},
        {q: grades[second][q]["correct"] for q in keep},
        _question,
    )


@pytest.mark.parametrize(
    ("model", "other", "single_table", "wins", "losses", "p"),
    [
        ("7b", "lexicon", False, 4, 0, 0.125),
        ("7b", "semantic", False, 8, 0, 0.0078),
        ("7b", "semantic-schema", False, 8, 0, 0.0078),
        ("7b", "catalog", True, 3, 1, 0.625),
        ("7b", "raw", True, 9, 1, 0.0215),
        ("7b", "raw", False, 9, 3, 0.146),
        ("1.5b", "lexicon", False, 1, 7, 0.0703),
    ],
)
def test_paired_comparisons_counted_once_per_question(model, other, single_table, wins, losses, p):
    result = _by_question(model, "lexicon+repair", other, single_table)
    assert (result.only_first, result.only_second) == (wins, losses)
    assert result.p_value == pytest.approx(p, abs=5e-4)


def test_both_languages_of_a_question_stand_or_fall_together():
    grades = _grades("7b")["lexicon+repair"]
    questions = {_question(q) for q in grades}
    agree = sum(grades[f"{q}-tr"]["correct"] == grades[f"{q}-en"]["correct"] for q in questions)
    assert (agree, len(questions)) == (20, 20)


def test_what_the_old_check_flagged_on_the_sql_arms_at_7b():
    grades = _grades("7b")
    flagged = [g for arm in ("raw", "catalog") for g in grades[arm].values() if g["hallucinated"]]
    assert {name for g in flagged for name in g["hallucinated"]} == {
        *("i", "m", "t1", "t2", "i1", "i2"),  # table aliases
        *("within", "month", "year", "current_date"),  # keywords
        *("percentile_cont", "date_sub"),  # functions
    }
    assert (len(flagged), sum(g["correct"] for g in flagged)) == (24, 10)


def test_q07_and_q15_were_answered_by_no_arm():
    for model in ("1.5b", "7b"):
        grades = _grades(model)
        for question in ("q07-tr", "q07-en", "q15-tr", "q15-en"):
            assert not any(arm[question]["correct"] for arm in grades.values())


def test_grading_is_positional():
    assert not rows_equal([(1.0, "mobil")], [("mobil", 1.0)])


class TestNamesAreReadFromTheParsedQuery:
    SCHEMA = ["islemler", "musteriler", "tutar", "kanal", "durum", "islem_tarihi", "segment"]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT i.kanal, SUM(i.tutar) FROM islemler i GROUP BY i.kanal",
            "WITH t AS (SELECT kanal FROM islemler) SELECT * FROM t WHERE kanal LIKE 'm%'",
            "SELECT strftime(islem_tarihi, '%Y') y, percentile_cont(0.5) WITHIN GROUP "
            "(ORDER BY tutar) FROM islemler WHERE islem_tarihi > current_date GROUP BY y",
            "SELECT EXTRACT(month FROM islem_tarihi), SUM(tutar) FROM islemler GROUP BY 1",
            "SELECT i1.kanal FROM islemler i1 JOIN islemler i2 ON i1.kanal = i2.kanal",
        ],
    )
    def test_aliases_keywords_and_functions_are_not_inventions(self, sql):
        assert hallucinated_identifiers(sql, self.SCHEMA) == set()

    def test_an_invented_table_or_column_still_is(self):
        sql = "SELECT i.canal, SUM(i.amount) FROM transactions i GROUP BY i.canal"
        assert hallucinated_identifiers(sql, self.SCHEMA) == {"canal", "amount", "transactions"}


class TestSignTestByCluster:
    def test_a_question_asked_twice_votes_once(self):
        first = {"q1-tr": True, "q1-en": True, "q2-tr": True, "q2-en": False}
        second = {"q1-tr": False, "q1-en": False, "q2-tr": True, "q2-en": False}
        result = sign_test_by_cluster(first, second, _question)
        assert (result.only_first, result.only_second, result.both, result.neither) == (1, 0, 1, 0)
        assert result.p_value == 1.0

    def test_the_arms_must_have_answered_the_same_questions(self):
        with pytest.raises(ValueError, match="same questions"):
            sign_test_by_cluster({"q1-tr": True}, {"q2-tr": True}, _question)
