"""Tests for grading an answer.

The risk here is a grader that is too strict — failing correct answers because
a driver returned Decimal, or because the rows came back in another order —
which would make every arm look worse and the comparison meaningless.
"""

from datetime import date, datetime
from decimal import Decimal

from bench.scoring import (
    Answer,
    grade,
    hallucinated_identifiers,
    identifiers_in,
    normalize_sql,
    rows_equal,
    values_equal,
)


class TestValueEquality:
    def test_a_decimal_matches_a_float(self):
        # Oracle returns Decimal where DuckDB returns float.
        assert values_equal(Decimal("10.50"), 10.5)

    def test_floats_that_differ_in_the_last_bits_match(self):
        # Summing money in a different order is not a wrong answer.
        assert values_equal(0.1 + 0.2, 0.3)

    def test_floats_that_actually_differ_do_not(self):
        assert not values_equal(10.0, 10.1)

    def test_a_date_matches_its_iso_text(self):
        assert values_equal(date(2024, 1, 1), "2024-01-01")

    def test_a_datetime_is_compared_without_a_timezone(self):
        assert values_equal(datetime(2024, 1, 1, 12, 0), "2024-01-01 12:00:00")

    def test_null_only_matches_null(self):
        assert values_equal(None, None)
        assert not values_equal(None, 0)
        assert not values_equal(0, None)

    def test_a_boolean_is_not_a_number(self):
        assert not values_equal(True, 1)

    def test_strings_ignore_surrounding_whitespace(self):
        assert values_equal(" paid ", "paid")


class TestResultSets:
    def test_row_order_is_ignored_by_default(self):
        assert rows_equal([(1,), (2,)], [(2,), (1,)])

    def test_row_order_counts_when_the_question_asks_for_it(self):
        assert not rows_equal([(1,), (2,)], [(2,), (1,)], ordered=True)
        assert rows_equal([(2,), (1,)], [(2,), (1,)], ordered=True)

    def test_duplicates_are_not_interchangeable(self):
        # A multiset comparison, not a set one: two rows of 1 is a different
        # answer from one row of 1 and one of 2.
        assert not rows_equal([(1,), (1,)], [(1,), (2,)])
        assert rows_equal([(1,), (1,)], [(1,), (1,)])

    def test_a_different_row_count_is_wrong(self):
        assert not rows_equal([(1,)], [(1,), (2,)])

    def test_a_different_column_count_is_wrong(self):
        assert not rows_equal([(1, 2)], [(1,)])

    def test_columns_are_compared_by_position_not_name(self):
        # Two arms may name a computed column differently and both be right.
        assert rows_equal([("paid", 100.0)], [("paid", Decimal("100"))])

    def test_two_empty_results_agree(self):
        assert rows_equal([], [])


class TestSqlNormalization:
    def test_whitespace_and_case_collapse(self):
        assert normalize_sql("SELECT   a\n FROM  t ;") == "select a from t"

    def test_string_literals_keep_their_case(self):
        # status = 'Paid' is a different query from status = 'paid'.
        assert normalize_sql("SELECT a FROM t WHERE s = 'Paid'").endswith("'Paid'")
        assert normalize_sql("select A from T where s = 'Paid'") == normalize_sql(
            "SELECT a FROM t WHERE s = 'Paid'"
        )


class TestHallucination:
    SCHEMA = ["islemler", "tutar", "durum", "musteri_id"]

    def test_a_column_the_schema_lacks_is_caught(self):
        assert hallucinated_identifiers("SELECT SUM(tutar), nope FROM islemler", self.SCHEMA) == {
            "nope"
        }

    def test_a_clean_query_reports_nothing(self):
        assert not hallucinated_identifiers(
            "SELECT SUM(tutar) FROM islemler WHERE durum = 'basarili'", self.SCHEMA
        )

    def test_an_alias_the_query_defines_is_not_an_invention(self):
        assert not hallucinated_identifiers(
            "SELECT SUM(tutar) AS toplam FROM islemler", self.SCHEMA
        )

    def test_keywords_are_not_identifiers(self):
        assert "select" not in identifiers_in("SELECT a FROM t")
        assert "group" not in identifiers_in("SELECT a FROM t GROUP BY a")

    def test_words_inside_string_literals_are_not_identifiers(self):
        # 'basarili' is data, not a column.
        assert "basarili" not in identifiers_in("SELECT a FROM t WHERE s = 'basarili'")


class TestGrading:
    GOLD = [(100.0,)]

    def test_matching_rows_are_correct(self):
        result = grade(
            Answer(rows=[(Decimal("100"),)], sql="SELECT SUM(tutar) FROM islemler"),
            self.GOLD,
            known_identifiers=["islemler", "tutar"],
        )
        assert result.correct
        assert not result.hallucinated_any

    def test_an_execution_error_is_simply_wrong(self):
        # From the asker's point of view a query that errors and one that
        # returns the wrong rows are the same non-answer.
        result = grade(
            Answer(sql="SELECT nope FROM islemler", error="no such column"),
            self.GOLD,
            known_identifiers=["islemler", "tutar"],
        )
        assert not result.correct
        assert result.error == "no such column"
        # The diagnosis survives: the failure is attributable.
        assert result.hallucinated == ("nope",)

    def test_exact_match_is_reported_separately_from_correctness(self):
        gold_sql = "SELECT SUM(tutar) FROM islemler"
        different = grade(
            Answer(rows=[(100.0,)], sql="select sum( tutar ) from islemler"),
            self.GOLD,
            gold_sql=gold_sql,
        )
        assert different.correct
        assert different.exact_match  # same query, differently spelled

        other = grade(
            Answer(rows=[(100.0,)], sql="SELECT SUM(t.tutar) FROM islemler t"),
            self.GOLD,
            gold_sql=gold_sql,
        )
        assert other.correct
        assert not other.exact_match  # correct, written differently

    def test_latency_is_carried_through(self):
        assert grade(Answer(rows=[(100.0,)], latency_ms=42.0), self.GOLD).latency_ms == 42.0

    def test_ordering_is_respected_when_asked_for(self):
        assert not grade(Answer(rows=[(2,), (1,)]), [(1,), (2,)], ordered=True).correct
        assert grade(Answer(rows=[(2,), (1,)]), [(1,), (2,)], ordered=False).correct


class TestStructuredAnswers:
    """An arm that answers with a request, not SQL, must be graded as such.

    Reading identifiers out of a JSON request as if it were SQL reported its
    keys — `table`, `metrics` — as invented columns, and so gave 100%
    hallucination to the one arm that structurally cannot invent a column.
    """

    SCHEMA = ["islemler", "tutar", "durum", "kanal"]

    def test_named_identifiers_are_taken_at_their_word(self):
        answer = Answer(
            rows=[(1.0,)],
            sql='{"table": "banka.islemler", "metrics": ["tutar:sum"]}',
            named=("islemler", "tutar"),
        )
        assert grade(answer, [(1.0,)], known_identifiers=self.SCHEMA).hallucinated == ()

    def test_a_name_the_schema_lacks_is_still_caught(self):
        answer = Answer(rows=[(1.0,)], sql="{}", named=("islemler", "nope"))
        assert grade(answer, [(1.0,)], known_identifiers=self.SCHEMA).hallucinated == ("nope",)

    def test_json_keys_are_not_mistaken_for_columns(self):
        answer = Answer(
            rows=[(1.0,)],
            sql='{"table": "banka.islemler", "metrics": ["tutar:sum"], "dimensions": []}',
            named=("islemler", "tutar"),
        )
        result = grade(answer, [(1.0,)], known_identifiers=self.SCHEMA)
        assert "table" not in result.hallucinated
        assert "metrics" not in result.hallucinated

    def test_exact_match_is_not_claimed_for_a_structured_answer(self):
        # There is no SQL text to compare against the gold statement.
        answer = Answer(rows=[(1.0,)], sql='{"metrics": ["tutar:sum"]}', named=("tutar",))
        result = grade(answer, [(1.0,)], gold_sql="SELECT SUM(tutar) FROM islemler")
        assert result.correct
        assert not result.exact_match

    def test_a_sql_arm_is_unaffected(self):
        answer = Answer(rows=[(1.0,)], sql="SELECT SUM(nope) FROM islemler")
        assert grade(answer, [(1.0,)], known_identifiers=self.SCHEMA).hallucinated == ("nope",)

    def test_a_refused_request_still_reports_what_it_named(self):
        answer = Answer(sql="{}", error="Unknown measure", named=("islemler", "gelir"))
        assert grade(answer, [], known_identifiers=self.SCHEMA).hallucinated == ("gelir",)


class TestFailureCategories:
    """Separating "the layer failed" from "the model chose badly"."""

    def categorise(self, **grade):
        from bench.analyse import categorise

        return categorise({"correct": False, "error": "", **grade})

    def test_a_correct_answer(self):
        from bench.analyse import categorise

        assert categorise({"correct": True, "error": ""}) == "correct"

    def test_ran_but_wrong_is_its_own_category(self):
        # The layer worked; the model asked for the wrong thing. That calls
        # for a better model, not a different layer.
        assert self.categorise() == "ran, but answered the wrong question"

    def test_a_syntax_mistake(self):
        assert self.categorise(error="Unknown transform 'sum'") == (
            "the language was written wrongly"
        )

    def test_naming_something_absent(self):
        assert self.categorise(error="Unknown measure 'gelir'") == (
            "named something that does not exist"
        )

    def test_a_refusal_is_not_a_defect(self):
        assert self.categorise(error="Refusing to load x (9,000,000 rows)") == (
            "refused: too much data"
        )

    def test_a_model_that_said_nothing_usable(self):
        assert self.categorise(error="model failed: Expecting value") == (
            "model produced no usable request"
        )

    def test_anything_else_is_other(self):
        assert self.categorise(error="the database caught fire") == "other"
