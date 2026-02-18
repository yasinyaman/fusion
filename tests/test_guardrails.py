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
