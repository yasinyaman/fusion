"""Contract for SqlValidator / SqlAnalyzer implementations."""

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import TableRef


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
