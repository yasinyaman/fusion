"""Shared pytest fixtures for Fusion tests."""

import pandas as pd
import pytest

from fusion import OLAPEngine, QueryCache, SchemaCatalog, SQLGuardrails


@pytest.fixture
def engine():
    """Fresh in-memory DuckDB engine for each test."""
    e = OLAPEngine(database=":memory:", threads=2, memory_limit="512MB")
    yield e
    e.close()


@pytest.fixture
def engine_with_data(engine):
    """Engine pre-loaded with sample data."""
    users = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
        "segment": ["premium", "basic", "premium", "standard", "basic"],
    })
    orders = pd.DataFrame({
        "id": [1, 2, 3, 4, 5, 6],
        "user_id": [1, 2, 1, 3, 4, 5],
        "amount": [100.0, 50.0, 200.0, 150.0, 75.0, 30.0],
        "product": ["A", "B", "A", "C", "B", "A"],
    })

    engine.execute_raw("CREATE SCHEMA IF NOT EXISTS test_db")
    with engine._lock:
        engine._conn.execute("CREATE TABLE test_db.users AS SELECT * FROM users")
        engine._conn.execute("CREATE TABLE test_db.orders AS SELECT * FROM orders")

    engine.catalog.register_source("test_db", "test", {
        "users": {
            "columns": [
                {"name": "id", "type": "int", "nullable": False},
                {"name": "name", "type": "varchar", "nullable": False},
                {"name": "segment", "type": "varchar", "nullable": True},
            ],
            "row_count": 5,
        },
        "orders": {
            "columns": [
                {"name": "id", "type": "int", "nullable": False},
                {"name": "user_id", "type": "int", "nullable": False},
                {"name": "amount", "type": "double", "nullable": False},
                {"name": "product", "type": "varchar", "nullable": True},
            ],
            "row_count": 6,
        },
    })

    # Mark tables as loaded (they were created directly above)
    engine.catalog.mark_loaded("test_db.users")
    engine.catalog.mark_loaded("test_db.orders")

    return engine


@pytest.fixture
def guardrails():
    return SQLGuardrails()


@pytest.fixture
def cache():
    return QueryCache(max_entries=10, default_ttl=60)


@pytest.fixture
def catalog():
    return SchemaCatalog()
