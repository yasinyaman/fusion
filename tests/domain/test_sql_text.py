"""Tests for parser-free SQL text helpers."""

import pytest

from fusion.domain.sql_text import (
    clean_sql,
    find_forbidden_function,
    has_multiple_statements,
    normalize_sql_for_cache,
    starts_with_dangerous_keyword,
    strip_comments,
    strip_string_literals,
)


class TestStripping:
    def test_strip_string_literals(self):
        assert strip_string_literals("a = 'x; y' AND b = \"q\"") == "a =  AND b = "

    def test_strip_comments_line_and_block(self):
        sql = "SELECT 1 -- comment; DROP\n/* block ; */ FROM t"
        assert strip_comments(sql) == "SELECT 1 \n FROM t"

    def test_unterminated_block_comment(self):
        assert strip_comments("SELECT 1 /* never closed") == "SELECT 1 "

    def test_clean_sql_combines(self):
        assert clean_sql("SELECT '--x' /* ; */ FROM t") == "SELECT   FROM t"


class TestMultiStatement:
    def test_detects_two_statements(self):
        assert has_multiple_statements("SELECT 1; DROP TABLE t")

    def test_trailing_semicolon_is_single(self):
        assert not has_multiple_statements("SELECT 1;")

    def test_semicolon_in_literal_or_comment_ignored(self):
        assert not has_multiple_statements("SELECT ';' -- ; DROP\n FROM t")


class TestForbiddenFunctions:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM READ_PARQUET('x')",
            "SELECT * FROM read_csv/**/('/etc/passwd')",
            "SELECT glob ('*')",
            "SELECT load('httpfs')",
        ],
    )
    def test_detected(self, sql):
        assert find_forbidden_function(sql) is not None

    def test_substring_in_column_name_is_allowed(self):
        assert find_forbidden_function("SELECT read_csv_count FROM t") is None

    def test_inside_string_literal_is_allowed(self):
        assert find_forbidden_function("SELECT * FROM t WHERE note = 'read_csv('") is None


class TestDangerousKeyword:
    def test_detects(self):
        assert starts_with_dangerous_keyword("  drop table t") == "DROP"

    def test_select_is_fine(self):
        assert starts_with_dangerous_keyword("SELECT 1") is None


class TestNormalizeForCache:
    def test_whitespace_and_case_outside_literals(self):
        a = normalize_sql_for_cache("select  *\n from t   where x = 1")
        b = normalize_sql_for_cache("SELECT * FROM t WHERE x = 1")
        assert a == b

    def test_normalize_keeps_literal_case(self):
        # Regression for the cache-key bug: literals must stay case-sensitive.
        lower = normalize_sql_for_cache("SELECT * FROM t WHERE name = 'alice'")
        upper = normalize_sql_for_cache("SELECT * FROM t WHERE name = 'ALICE'")
        assert lower != upper
        assert "'alice'" in lower

    def test_quoted_identifiers_kept_verbatim(self):
        assert normalize_sql_for_cache('select "MyCol" from t') == 'SELECT "MyCol" FROM T'

    def test_whitespace_inside_literal_preserved(self):
        assert normalize_sql_for_cache("SELECT 'a   b'") == "SELECT 'a   b'"

    def test_escaped_quote_inside_literal(self):
        # 'it''s' is one literal with an escaped quote; the second quote pair
        # re-enters literal mode so the content stays verbatim.
        assert normalize_sql_for_cache("select 'it''s'") == "SELECT 'it''s'"
