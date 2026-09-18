"""Tests for the sqlglot validator (guardrails) and analyzer."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import TableRef
from fusion.domain.query_shape import JoinEquality
from fusion.domain.slices import Predicate


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


class TestAnalyze:
    """``analyze`` must never claim a predicate that could change the result."""

    def test_single_table_columns_and_predicates(self, analyzer):
        shape = analyzer.analyze(
            "SELECT id, total FROM db.orders WHERE status = 'new' AND total > 10"
        )
        assert shape.is_simple_select and shape.single_table
        use = shape.use_for(TableRef("db", "orders"))
        assert use.alias == "orders"
        assert use.columns == frozenset({"id", "total", "status"})
        assert set(use.predicates) == {
            Predicate("status", "eq", "new"),
            Predicate("total", "gt", 10),
        }

    def test_unqualified_table_keeps_an_empty_source(self, analyzer):
        shape = analyzer.analyze("SELECT id FROM orders WHERE id = 1")
        assert shape.use_for(TableRef("", "orders")).predicates == (Predicate("id", "eq", 1),)

    @pytest.mark.parametrize(
        ("where", "expected"),
        [
            ("a = 'x'", Predicate("a", "eq", "x")),
            ("a != 'x'", Predicate("a", "ne", "x")),
            ("a <> 'x'", Predicate("a", "ne", "x")),
            ("n > 5", Predicate("n", "gt", 5)),
            ("n >= 5", Predicate("n", "gte", 5)),
            ("n < 5", Predicate("n", "lt", 5)),
            ("n <= 5", Predicate("n", "lte", 5)),
            ("n > -5", Predicate("n", "gt", -5)),
            ("n > 1.5", Predicate("n", "gt", 1.5)),
            ("5 < n", Predicate("n", "gt", 5)),
            ("'x' = a", Predicate("a", "eq", "x")),
            ("a LIKE '%x%'", Predicate("a", "like", "%x%")),
            ("a IN (1, 2)", Predicate("a", "in", (1, 2))),
            ("a IN ('x')", Predicate("a", "in", ("x",))),
            ("a IS NULL", Predicate("a", "is_null", True)),
            ("flag = TRUE", Predicate("flag", "eq", True)),
        ],
    )
    def test_pushable_conditions(self, analyzer, where, expected):
        shape = analyzer.analyze(f"SELECT x FROM db.t WHERE {where}")
        assert shape.use_for(TableRef("db", "t")).predicates == (expected,)

    @pytest.mark.parametrize(
        "where",
        [
            "a = 'x' OR b = 'y'",
            "NOT (a = 'x')",
            "a IS NOT NULL",
            "LOWER(a) = 'x'",
            "a = b",
            "a = ?",
            "a > CURRENT_DATE",
            "a IN (SELECT id FROM db.u)",
            "a::text = 'x'",
            "a BETWEEN 1 AND 5",
            "EXISTS (SELECT 1 FROM db.u)",
        ],
    )
    def test_conditions_that_are_never_pushed(self, analyzer, where):
        shape = analyzer.analyze(f"SELECT x FROM db.t WHERE {where}")
        if shape.is_simple_select:
            assert shape.use_for(TableRef("db", "t")).predicates == ()

    def test_and_chains_are_split_but_or_branches_are_dropped(self, analyzer):
        shape = analyzer.analyze("SELECT x FROM db.t WHERE a = 1 AND (b = 2 OR c = 3) AND d = 4")
        assert set(shape.use_for(TableRef("db", "t")).predicates) == {
            Predicate("a", "eq", 1),
            Predicate("d", "eq", 4),
        }

    def test_star_means_every_column(self, analyzer):
        shape = analyzer.analyze("SELECT * FROM db.t WHERE a = 1")
        use = shape.use_for(TableRef("db", "t"))
        assert use.columns is None
        assert use.predicates == (Predicate("a", "eq", 1),)  # the filter still holds

    def test_qualified_star_only_widens_its_own_table(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.*, u.name FROM db.orders o JOIN db.users u ON o.user_id = u.id"
        )
        assert shape.use_for(TableRef("db", "orders")).columns is None
        assert shape.use_for(TableRef("db", "users")).columns == frozenset({"name", "id"})

    def test_count_star_does_not_widen_the_projection(self, analyzer):
        shape = analyzer.analyze("SELECT COUNT(*) AS c FROM db.t WHERE a = 1")
        assert shape.use_for(TableRef("db", "t")).columns == frozenset({"a"})

    def test_ambiguous_unqualified_column_widens_everything(self, analyzer):
        shape = analyzer.analyze(
            "SELECT name FROM db.orders o JOIN db.users u ON o.user_id = u.id WHERE total > 1"
        )
        assert all(use.columns is None for use in shape.tables)
        assert all(use.predicates == () for use in shape.tables)

    def test_inner_join_equality_and_per_table_predicates(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.id, u.name FROM db.orders o JOIN db.users u ON o.user_id = u.id "
            "WHERE u.segment = 'premium' AND o.total > 100"
        )
        assert shape.joins == (JoinEquality("o", "user_id", "u", "id", inner=True),)
        assert shape.use_for(TableRef("db", "orders")).predicates == (
            Predicate("total", "gt", 100),
        )
        assert shape.use_for(TableRef("db", "users")).predicates == (
            Predicate("segment", "eq", "premium"),
        )
        assert shape.inner_joins_for("u") == shape.joins

    def test_inner_join_on_clause_can_carry_a_filter(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.id FROM db.orders o JOIN db.users u ON o.user_id = u.id "
            "AND u.segment = 'premium'"
        )
        assert shape.use_for(TableRef("db", "users")).predicates == (
            Predicate("segment", "eq", "premium"),
        )

    def test_left_join_never_filters_the_null_side(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.id FROM db.orders o LEFT JOIN db.users u ON o.user_id = u.id "
            "AND u.segment = 'premium' WHERE u.zip = '1' AND o.total > 1"
        )
        users = shape.use_for(TableRef("db", "users"))
        assert users.outer_null_side and users.predicates == ()
        assert shape.use_for(TableRef("db", "orders")).predicates == (Predicate("total", "gt", 1),)
        assert shape.joins[0].inner is False

    def test_right_join_marks_the_left_tables(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.id FROM db.orders o RIGHT JOIN db.users u ON o.user_id = u.id "
            "WHERE o.total > 1"
        )
        assert shape.use_for(TableRef("db", "orders")).outer_null_side is True
        assert shape.use_for(TableRef("db", "orders")).predicates == ()
        assert shape.use_for(TableRef("db", "users")).outer_null_side is False

    def test_full_join_marks_both_sides(self, analyzer):
        shape = analyzer.analyze(
            "SELECT o.id FROM db.orders o FULL JOIN db.users u ON o.user_id = u.id"
        )
        assert all(use.outer_null_side for use in shape.tables)

    def test_limit_is_reported_when_literal(self, analyzer):
        assert analyzer.analyze("SELECT a FROM db.t LIMIT 10").limit == 10
        assert analyzer.analyze("SELECT a FROM db.t").limit is None

    def test_self_join_gives_up_on_per_alias_slicing(self, analyzer):
        shape = analyzer.analyze(
            "SELECT a.id FROM db.t a JOIN db.t b ON a.parent = b.id WHERE a.x = 1"
        )
        assert all(use.is_unrestricted for use in shape.tables)

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH c AS (SELECT 1 AS x) SELECT x FROM c",
            "SELECT a FROM db.t UNION SELECT a FROM db.u",
            "SELECT a FROM (SELECT a FROM db.t) s",
            "SELECT a, ROW_NUMBER() OVER (PARTITION BY b) FROM db.t",
            "SELECT a FROM db.t WHERE a IN (SELECT id FROM db.u)",
        ],
    )
    def test_complex_sql_is_not_analyzed(self, analyzer, sql):
        shape = analyzer.analyze(sql)
        assert shape.is_simple_select is False
        assert shape.tables == ()

    def test_unparseable_sql_is_not_analyzed(self, analyzer):
        assert analyzer.analyze("this is not sql at all ((").is_simple_select is False

    def test_select_without_a_from_clause(self, analyzer):
        shape = analyzer.analyze("SELECT 1 AS x")
        assert shape.is_simple_select and shape.tables == ()


class TestRewriteTables:
    def test_points_a_table_at_its_slice_and_keeps_the_name_usable(self, analyzer):
        sql = "SELECT orders.id FROM db.orders WHERE orders.status = 'new'"
        out = analyzer.rewrite_tables(sql, {TableRef("db", "orders"): "db.orders__s_abc"})
        assert "db.orders__s_abc" in out
        assert "AS orders" in out or '"orders"' in out
        assert "orders.status" in out.replace('"', "")

    def test_keeps_an_existing_alias(self, analyzer):
        out = analyzer.rewrite_tables(
            "SELECT o.id FROM db.orders o", {TableRef("db", "orders"): "db.orders__s_abc"}
        )
        assert "db.orders__s_abc AS o" in out.replace('"', "")

    def test_rewrites_only_the_mapped_tables(self, analyzer):
        out = analyzer.rewrite_tables(
            "SELECT o.id, u.name FROM db.orders o JOIN db.users u ON o.user_id = u.id",
            {TableRef("db", "orders"): "db.orders__s_abc"},
        )
        plain = out.replace('"', "")
        assert "db.orders__s_abc AS o" in plain and "db.users AS u" in plain

    def test_unqualified_target_drops_the_schema(self, analyzer):
        out = analyzer.rewrite_tables(
            "SELECT id FROM db.orders", {TableRef("db", "orders"): "slice_tbl"}
        )
        plain = out.replace('"', "")
        assert "FROM slice_tbl AS orders" in plain and "db." not in plain

    def test_no_mapping_returns_the_query_untouched(self, analyzer):
        sql = "SELECT id FROM db.orders"
        assert analyzer.rewrite_tables(sql, {}) == sql
        assert analyzer.rewrite_tables(sql, {TableRef("db", "users"): "x"}) == sql
        assert analyzer.rewrite_tables(sql, {TableRef("db", "orders"): "orders"}) == sql

    def test_unparseable_sql_is_returned_unchanged(self, analyzer):
        assert analyzer.rewrite_tables("((", {TableRef("", "t"): "u"}) == "(("

    def test_where_clause_still_filters_after_the_rewrite(self, analyzer):
        # The slice only narrows the scan; the original conditions must remain.
        out = analyzer.rewrite_tables(
            "SELECT id FROM db.orders WHERE status = 'new' AND total > 10",
            {TableRef("db", "orders"): "db.orders__s_abc"},
        )
        assert "status = 'new'" in out and "total > 10" in out
