"""Tests for the sqlglot validator (guardrails) and analyzer."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import TableRef


@pytest.fixture
def validator():
    return SqlglotValidator()


@pytest.fixture
def analyzer():
    return SqlglotAnalyzer()


class TestValidatorAllows:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM users",
            "SELECT * FROM users WHERE id = 1",
            "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte",
            """
            SELECT u.name, COUNT(*) as cnt, SUM(o.amount) as total
            FROM users u JOIN orders o ON u.id = o.user_id
            GROUP BY u.name HAVING COUNT(*) > 1 ORDER BY total DESC LIMIT 10
            """,
            "EXPLAIN SELECT 1",
            "SELECT COUNT(*) FROM orders WHERE amount > 100",
            "SELECT read_count FROM metrics",
            "SELECT * FROM logs WHERE message = 'tried read_csv( here'",
        ],
    )
    def test_allowed(self, validator, sql):
        validator.validate(sql)


class TestSetOperations:
    """Regression: UNION/INTERSECT/EXCEPT and parenthesised selects are queries."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 UNION SELECT 2",
            "SELECT 1 UNION ALL SELECT 2",
            "SELECT 1 INTERSECT SELECT 1",
            "SELECT 1 EXCEPT SELECT 2",
            "(SELECT 1)",
            "WITH a AS (SELECT 1 x) SELECT x FROM a UNION SELECT 2",
            "SELECT * FROM (SELECT 1 UNION SELECT 2) t",
        ],
    )
    def test_set_operations_allowed(self, validator, sql):
        validator.validate(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "VALUES (1)",
            "CREATE TABLE x AS SELECT 1",
            "SELECT 1 UNION SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * INTO new_table FROM users",
        ],
    )
    def test_non_queries_still_blocked(self, validator, sql):
        with pytest.raises(GuardrailViolation):
            validator.validate(sql)


class TestValidatorBlocks:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "DELETE FROM orders WHERE 1=1",
            "INSERT INTO users VALUES (1, 'hack')",
            "UPDATE users SET name = 'hack'",
            "ALTER TABLE users ADD COLUMN evil TEXT",
            "TRUNCATE TABLE users",
            "SELECT * FROM users; DROP TABLE users; --",
            "",
            "   ",
        ],
    )
    def test_blocked(self, validator, sql):
        with pytest.raises(GuardrailViolation):
            validator.validate(sql)

    def test_allows_create_mv_when_enabled(self):
        SqlglotValidator(allow_create_mv=True).validate("CREATE TABLE mv_daily AS (SELECT 1)")

    def test_blocks_create_non_mv(self):
        with pytest.raises(GuardrailViolation):
            SqlglotValidator(allow_create_mv=True).validate("CREATE TABLE evil AS (SELECT 1)")

    def test_blocks_create_mv_by_default(self, validator):
        with pytest.raises(GuardrailViolation):
            validator.validate("CREATE TABLE mv_daily AS (SELECT 1)")


class TestForbiddenFunctions:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM read_csv_auto('/etc/passwd')",
            "SELECT * FROM read_parquet('s3://bucket/key')",
            "SELECT * FROM read_json_auto('/etc/passwd')",
            "SELECT * FROM read_text('/etc/passwd')",
            "SELECT * FROM glob('/etc/*')",
            "SELECT load('httpfs')",
            "SELECT install('httpfs')",
            "WITH x AS (SELECT * FROM read_json_auto('/etc/passwd')) SELECT * FROM x",
            "SELECT * FROM ReAd_CsV ( '/etc/passwd' )",
            "SELECT * FROM read_csv/**/('/etc/passwd')",
        ],
    )
    def test_blocks_forbidden_functions(self, validator, sql):
        with pytest.raises(GuardrailViolation):
            validator.validate(sql)


class TestAdversarialMatrix:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE users",
            "SELECT 1;SELECT 2",
            "SELECT 1 -- harmless\n; DROP TABLE users",
            "SELECT name FROM users UNION SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM (SELECT * FROM read_parquet('x')) t",
            "SELECT * FROM users WHERE id IN (SELECT id FROM read_csv('/x'))",
            "ATTACH 'evil.db'",
            "ATTACH 'evil.db' AS e",
            "COPY users TO '/tmp/x.csv'",
            "PRAGMA database_list",
            "INSTALL httpfs",
            "LOAD httpfs",
            "SeLeCt * FrOm ReAd_PaRqUeT('x')",
            "SET enable_external_access = true",
            "CALL pragma_version()",
        ],
    )
    def test_blocked(self, validator, sql):
        with pytest.raises(GuardrailViolation):
            validator.validate(sql)


class TestAnalyzer:
    def test_qualified_and_unqualified_refs(self, analyzer):
        refs = analyzer.table_references("SELECT * FROM warp.orders o JOIN users u ON 1=1")
        assert refs == [TableRef("warp", "orders"), TableRef("", "users")]

    def test_cte_names_excluded(self, analyzer):
        sql = "WITH ranked AS (SELECT * FROM warp.orders) SELECT * FROM ranked WHERE rn <= 10"
        assert analyzer.table_references(sql) == [TableRef("warp", "orders")]

    def test_dedupes_and_handles_subqueries(self, analyzer):
        sql = "SELECT * FROM a.t WHERE id IN (SELECT id FROM a.t) UNION SELECT * FROM b.u"
        assert analyzer.table_references(sql) == [TableRef("a", "t"), TableRef("b", "u")]

    def test_garbage_sql_yields_nothing_useful(self, analyzer):
        refs = analyzer.table_references("THIS IS NOT SQL AT ALL")
        assert all(isinstance(r, TableRef) for r in refs)

    def test_strip_source_prefix(self, analyzer):
        out = analyzer.strip_source_prefix("SELECT * FROM warp_main.orders", "warp_main")
        assert out == "SELECT * FROM orders"

    def test_strip_source_prefix_join_keeps_other_sources(self, analyzer):
        sql = "SELECT o.id FROM warp_main.orders AS o JOIN other.users AS u ON o.uid = u.id"
        out = analyzer.strip_source_prefix(sql, "warp_main")
        assert "warp_main." not in out
        assert "other.users" in out
