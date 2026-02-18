"""Tests for OLAPEngine."""

import pytest

from fusion.engine import OLAPEngine
from fusion.exceptions import GuardrailViolation, QueryError


class TestOLAPEngine:
    def test_engine_creation(self, engine):
        assert engine is not None

    def test_context_manager(self):
        with OLAPEngine(database=":memory:") as e:
            result = e.sql("SELECT 1 AS x")
            assert result.row_count == 1

    def test_simple_query(self, engine):
        result = engine.sql("SELECT 1 AS x, 2 AS y")
        assert result.row_count == 1
        assert result.columns == ["x", "y"]

    def test_query_with_data(self, engine_with_data):
        result = engine_with_data.sql("SELECT * FROM test_db.users ORDER BY id")
        assert result.row_count == 5
        d = result.to_dict()
        assert d[0]["name"] == "Alice"

    def test_query_caching(self, engine_with_data):
        sql = "SELECT COUNT(*) as cnt FROM test_db.users"
        r1 = engine_with_data.sql(sql)
        r2 = engine_with_data.sql(sql)
        assert r1.from_cache is False
        assert r2.from_cache is True

    def test_query_no_cache(self, engine_with_data):
        sql = "SELECT COUNT(*) as cnt FROM test_db.users"
        r1 = engine_with_data.sql(sql, use_cache=False)
        r2 = engine_with_data.sql(sql, use_cache=False)
        assert r1.from_cache is False
        assert r2.from_cache is False

    def test_guardrails_block_drop(self, engine):
        with pytest.raises(GuardrailViolation):
            engine.sql("DROP TABLE users")

    def test_guardrails_block_delete(self, engine):
        with pytest.raises(GuardrailViolation):
            engine.sql("DELETE FROM users WHERE 1=1")

    def test_query_error_on_bad_sql(self, engine):
        with pytest.raises(QueryError):
            engine.sql("SELECT * FROM nonexistent_table_xyz")

    def test_cache_stats(self, engine_with_data):
        engine_with_data.sql("SELECT 1")
        engine_with_data.sql("SELECT 1")  # cache hit
        stats = engine_with_data.cache_stats()
        assert stats["hits"] >= 1

    def test_schema_context(self, engine_with_data):
        context = engine_with_data.schema_context()
        assert "test_db" in context
        assert "users" in context

    def test_table_stats(self, engine_with_data):
        stats = engine_with_data.table_stats()
        assert stats["test_db.users"] == 5
        assert stats["test_db.orders"] == 6

    def test_join_query(self, engine_with_data):
        result = engine_with_data.sql("""
            SELECT u.name, SUM(o.amount) as total
            FROM test_db.users u
            JOIN test_db.orders o ON u.id = o.user_id
            GROUP BY u.name
            ORDER BY total DESC
        """)
        assert result.row_count > 0
        d = result.to_dict()
        names = [row["name"] for row in d]
        assert "Alice" in names
