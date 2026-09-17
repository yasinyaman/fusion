"""Contract for SqlValidator / SqlAnalyzer implementations."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import TableRef
from fusion.domain.slices import Predicate


@pytest.fixture(params=[SqlglotValidator], ids=["sqlglot"])
def validator(request):
    return request.param()


@pytest.fixture(params=[SqlglotAnalyzer], ids=["sqlglot"])
def analyzer(request):
    return request.param()


class TestSqlValidatorContract:
    def test_select_passes(self, validator):
        validator.validate("SELECT 1")

    @pytest.mark.parametrize(
        "sql", ["DROP TABLE t", "INSERT INTO t VALUES (1)", "SELECT 1; SELECT 2"]
    )
    def test_non_select_raises(self, validator, sql):
        with pytest.raises(GuardrailViolation):
            validator.validate(sql)


class TestSqlAnalyzerContract:
    def test_table_references(self, analyzer):
        refs = analyzer.table_references("SELECT * FROM s.t JOIN u ON 1=1")
        assert TableRef("s", "t") in refs
        assert TableRef("", "u") in refs

    def test_strip_source_prefix(self, analyzer):
        assert "s." not in analyzer.strip_source_prefix("SELECT * FROM s.t", "s")

    def test_analyze_reports_a_shape_for_a_simple_select(self, analyzer):
        shape = analyzer.analyze("SELECT id FROM s.t WHERE status = 'new'")
        assert shape.is_simple_select
        use = shape.use_for(TableRef("s", "t"))
        assert use is not None
        assert use.columns == frozenset({"id", "status"})
        assert use.predicates == (Predicate("status", "eq", "new"),)

    def test_analyze_refuses_to_guess(self, analyzer):
        assert analyzer.analyze("WITH c AS (SELECT 1 AS x) SELECT x FROM c").is_simple_select is (
            False
        )
        assert analyzer.analyze("not sql ((").is_simple_select is False

    def test_analyzed_predicates_only_ever_narrow_the_source_read(self, analyzer):
        # Whatever the analyzer reports must be implied by the query itself:
        # every row it excludes is a row the WHERE clause excludes anyway.
        shape = analyzer.analyze("SELECT * FROM s.t WHERE a = 1 OR b = 2")
        use = shape.use_for(TableRef("s", "t"))
        assert use is None or use.predicates == ()

    def test_rewrite_tables_redirects_and_keeps_the_name_as_alias(self, analyzer):
        out = analyzer.rewrite_tables("SELECT t.id FROM s.t", {TableRef("s", "t"): "s.t__s_1"})
        plain = out.replace('"', "")
        assert "s.t__s_1" in plain and "AS t" in plain
        assert analyzer.rewrite_tables("SELECT 1 FROM s.t", {}) == "SELECT 1 FROM s.t"
