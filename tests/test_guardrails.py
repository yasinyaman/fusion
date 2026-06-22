"""Tests for SQL guardrails."""

import pytest

from fusion.exceptions import GuardrailViolation
from fusion.guardrails import SQLGuardrails


class TestSQLGuardrails:
    def test_allows_select(self, guardrails):
        assert guardrails.validate("SELECT * FROM users") is True

    def test_allows_select_with_where(self, guardrails):
        assert guardrails.validate("SELECT * FROM users WHERE id = 1") is True

    def test_allows_cte(self, guardrails):
        sql = "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"
        assert guardrails.validate(sql) is True

    def test_allows_complex_select(self, guardrails):
        sql = """
        SELECT u.name, COUNT(*) as cnt, SUM(o.amount) as total
        FROM users u
        JOIN orders o ON u.id = o.user_id
        GROUP BY u.name
        HAVING COUNT(*) > 1
        ORDER BY total DESC
        LIMIT 10
        """
        assert guardrails.validate(sql) is True

    def test_blocks_drop(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("DROP TABLE users")

    def test_blocks_delete(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("DELETE FROM orders WHERE 1=1")

    def test_blocks_insert(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("INSERT INTO users VALUES (1, 'hack')")

    def test_blocks_update(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("UPDATE users SET name = 'hack'")

    def test_blocks_alter(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("ALTER TABLE users ADD COLUMN evil TEXT")

    def test_blocks_truncate(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("TRUNCATE TABLE users")

    def test_blocks_multi_statement(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("SELECT * FROM users; DROP TABLE users; --")

    def test_blocks_empty_query(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("")

    def test_blocks_whitespace_only(self, guardrails):
        with pytest.raises(GuardrailViolation):
            guardrails.validate("   ")

    def test_allows_create_mv_when_enabled(self):
        g = SQLGuardrails(allow_create_mv=True)
        assert g.validate("CREATE TABLE mv_daily AS (SELECT 1)") is True

    def test_blocks_create_non_mv(self):
        g = SQLGuardrails(allow_create_mv=True)
        with pytest.raises(GuardrailViolation):
            g.validate("CREATE TABLE evil AS (SELECT 1)")


class TestForbiddenFunctions:
    """Dangerous file/network/extension functions must be blocked even when
    they appear inside an otherwise-valid SELECT."""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://bucket/key')",
        "SELECT * FROM read_json_auto('/etc/passwd')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT load('httpfs')",
        "SELECT install('httpfs')",
        "WITH x AS (SELECT * FROM read_json_auto('/etc/passwd')) SELECT * FROM x",
        "SELECT * FROM ReAd_CsV ( '/etc/passwd' )",          # case + spacing
        "SELECT * FROM read_csv/**/('/etc/passwd')",          # comment evasion
    ])
    def test_blocks_forbidden_functions(self, guardrails, sql):
        with pytest.raises(GuardrailViolation):
            guardrails.validate(sql)

    def test_substring_in_column_name_is_allowed(self, guardrails):
        # `read_count` contains "read_c" but is not a function call
        assert guardrails.validate("SELECT read_count FROM metrics") is True

    def test_forbidden_name_inside_string_literal_is_allowed(self, guardrails):
        # The function name only appears inside a string value, not as a call
        sql = "SELECT * FROM logs WHERE message = 'tried read_csv( here'"
        assert guardrails.validate(sql) is True


class TestAdversarialMatrix:
    """Attack matrix from the security audit — every entry must be blocked."""

    @pytest.mark.parametrize("sql", [
        # Stacked / multi-statement injection
        "SELECT 1; DROP TABLE users",
        "SELECT 1;SELECT 2",
        "SELECT 1 -- harmless\n; DROP TABLE users",
        # Dangerous function smuggled through set ops / subqueries
        "SELECT name FROM users UNION SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM (SELECT * FROM read_parquet('x')) t",
        "SELECT * FROM users WHERE id IN (SELECT id FROM read_csv('/x'))",
        # File / DB attach / extension statements
        "ATTACH 'evil.db'",
        "ATTACH 'evil.db' AS e",
        "COPY users TO '/tmp/x.csv'",
        "PRAGMA database_list",
        "INSTALL httpfs",
        "LOAD httpfs",
        # Case-obfuscated function call
        "SeLeCt * FrOm ReAd_PaRqUeT('x')",
    ])
    def test_blocked(self, guardrails, sql):
        with pytest.raises(GuardrailViolation):
            guardrails.validate(sql)

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM users",
        "WITH x AS (SELECT 1 AS c) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "SELECT COUNT(*) FROM orders WHERE amount > 100",
    ])
    def test_legitimate_queries_still_pass(self, guardrails, sql):
        assert guardrails.validate(sql) is True
