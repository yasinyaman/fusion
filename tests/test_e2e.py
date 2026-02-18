"""End-to-end tests — full stack from mock Warp API to REST/tool responses.

Simulates a realistic Warp REST API server (mock HTTP), connects via
WarpConnector → OLAPEngine → ToolExecutor → REST API, and verifies
complete user scenarios including lazy loading, pushdown, caching,
materialized views, and error handling.
"""

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from fusion.engine import OLAPEngine
from fusion.tools.executor import ToolExecutor


# ---------------------------------------------------------------------------
# Mock Warp HTTP layer
# ---------------------------------------------------------------------------

# Simulated database tables (the "Warp backend")
MOCK_USERS = [
    {"id": 1, "name": "Alice", "email": "alice@test.com", "segment": "premium"},
    {"id": 2, "name": "Bob", "email": "bob@test.com", "segment": "basic"},
    {"id": 3, "name": "Charlie", "email": "charlie@test.com", "segment": "premium"},
    {"id": 4, "name": "Diana", "email": "diana@test.com", "segment": "standard"},
    {"id": 5, "name": "Eve", "email": "eve@test.com", "segment": "basic"},
]

MOCK_ORDERS = [
    {"id": 1, "user_id": 1, "amount": 250.0, "product": "Laptop", "status": "completed"},
    {"id": 2, "user_id": 2, "amount": 50.0, "product": "Mouse", "status": "completed"},
    {"id": 3, "user_id": 1, "amount": 120.0, "product": "Keyboard", "status": "pending"},
    {"id": 4, "user_id": 3, "amount": 800.0, "product": "Monitor", "status": "completed"},
    {"id": 5, "user_id": 4, "amount": 35.0, "product": "Cable", "status": "cancelled"},
    {"id": 6, "user_id": 5, "amount": 15.0, "product": "Mouse", "status": "completed"},
    {"id": 7, "user_id": 1, "amount": 300.0, "product": "SSD", "status": "completed"},
    {"id": 8, "user_id": 3, "amount": 60.0, "product": "Webcam", "status": "pending"},
]

MOCK_PRODUCTS = [
    {"id": 1, "name": "Laptop", "category": "electronics", "price": 250.0},
    {"id": 2, "name": "Mouse", "category": "accessories", "price": 50.0},
    {"id": 3, "name": "Keyboard", "category": "accessories", "price": 120.0},
    {"id": 4, "name": "Monitor", "category": "electronics", "price": 800.0},
    {"id": 5, "name": "Cable", "category": "accessories", "price": 35.0},
    {"id": 6, "name": "SSD", "category": "storage", "price": 300.0},
    {"id": 7, "name": "Webcam", "category": "accessories", "price": 60.0},
]

MOCK_DB = {
    "users": MOCK_USERS,
    "orders": MOCK_ORDERS,
    "products": MOCK_PRODUCTS,
}


def _make_mock_response(data, status_code=200):
    """Create a mock requests.Response with JSON data."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = data
    return resp


def _warp_get_handler(url, params=None, timeout=None):
    """Simulate Warp GET requests (health, info, table data)."""
    if "/health" in url:
        return _make_mock_response({"status": "ok"})

    if "/info" in url:
        return _make_mock_response({
            "databases": {
                "ecommerce": {
                    "tables": list(MOCK_DB.keys()),
                    "table_count": len(MOCK_DB),
                }
            }
        })

    # Table data endpoint: /api/v1/{db}/{table}?limit=X&offset=Y
    for table_name, table_data in MOCK_DB.items():
        if f"/{table_name}" in url:
            offset = (params or {}).get("offset", 0)
            limit = (params or {}).get("limit", 1000)
            rows = table_data[offset:offset + limit]
            return _make_mock_response(rows)

    return _make_mock_response([], status_code=404)


def _warp_post_handler(url, json=None, timeout=None):
    """Simulate Warp POST /query/execute — run SQL against mock data."""
    if "/query/execute" not in url:
        return _make_mock_response({"error": "not found"}, status_code=404)

    query = (json or {}).get("query", "")
    sql_upper = query.upper().strip()

    # Simple SQL interpreter for mock data
    # This handles the common patterns our tools generate

    # COUNT(*) queries
    if "COUNT(*)" in sql_upper:
        for table_name, table_data in MOCK_DB.items():
            if table_name.upper() in sql_upper:
                if "WHERE" in sql_upper:
                    # Filter count
                    df = pd.DataFrame(table_data)
                    filtered = _apply_simple_where(df, query)
                    return _make_mock_response({"data": [{"count": len(filtered)}]})
                return _make_mock_response({"data": [{"count": len(table_data)}]})
        return _make_mock_response({"data": [{"count": 0}]})

    # GROUP BY aggregation
    if "GROUP BY" in sql_upper:
        for table_name, table_data in MOCK_DB.items():
            if table_name.upper() in sql_upper:
                df = pd.DataFrame(table_data)
                result = _execute_aggregate(df, query)
                return _make_mock_response({"data": result})
        return _make_mock_response({"data": []})

    # SELECT with WHERE
    if "WHERE" in sql_upper:
        for table_name, table_data in MOCK_DB.items():
            if table_name.upper() in sql_upper:
                df = pd.DataFrame(table_data)
                filtered = _apply_simple_where(df, query)
                # Apply LIMIT
                limit = _extract_limit(query)
                if limit:
                    filtered = filtered.head(limit)
                return _make_mock_response({"data": filtered.to_dict(orient="records")})
        return _make_mock_response({"data": []})

    # Simple SELECT *
    for table_name, table_data in MOCK_DB.items():
        if table_name.upper() in sql_upper:
            limit = _extract_limit(query)
            data = table_data[:limit] if limit else table_data
            return _make_mock_response({"data": data})

    return _make_mock_response({"data": []})


def _apply_simple_where(df, query):
    """Apply a simple WHERE clause to a DataFrame (basic parser)."""
    import re

    upper = query.upper()
    where_idx = upper.index("WHERE") + 5
    # Find the end of WHERE clause (before GROUP BY, ORDER BY, LIMIT)
    end_idx = len(query)
    for keyword in ["GROUP BY", "ORDER BY", "LIMIT"]:
        idx = upper.find(keyword, where_idx)
        if idx != -1:
            end_idx = min(end_idx, idx)

    where_clause = query[where_idx:end_idx].strip()

    # Handle CAST(column AS VARCHAR) = 'value'
    cast_match = re.search(
        r"CAST\((\w+)\s+AS\s+VARCHAR\)\s*=\s*'([^']*)'",
        where_clause,
        re.IGNORECASE,
    )
    if cast_match:
        col, val = cast_match.group(1), cast_match.group(2)
        if col in df.columns:
            return df[df[col].astype(str) == val]

    # Handle CAST(column AS VARCHAR) LIKE 'value'
    like_match = re.search(
        r"CAST\((\w+)\s+AS\s+VARCHAR\)\s+LIKE\s+'([^']*)'",
        where_clause,
        re.IGNORECASE,
    )
    if like_match:
        col, pattern = like_match.group(1), like_match.group(2)
        regex_pattern = pattern.replace("%", ".*")
        if col in df.columns:
            return df[df[col].astype(str).str.match(regex_pattern, case=False)]

    # Handle column = 'value'
    eq_match = re.search(r"(\w+)\s*=\s*'([^']*)'", where_clause)
    if eq_match:
        col, val = eq_match.group(1), eq_match.group(2)
        if col in df.columns:
            return df[df[col].astype(str) == val]

    # Handle column = number
    num_match = re.search(r"(\w+)\s*=\s*(\d+(?:\.\d+)?)", where_clause)
    if num_match:
        col, val = num_match.group(1), float(num_match.group(2))
        if col in df.columns:
            return df[df[col] == val]

    return df


def _execute_aggregate(df, query):
    """Execute a GROUP BY aggregation query (basic parser)."""
    import re

    upper = query.upper()

    # Extract GROUP BY column
    gb_match = re.search(r"GROUP\s+BY\s+(\w+)", query, re.IGNORECASE)
    if not gb_match:
        return []
    group_col = gb_match.group(1)

    # Extract aggregation: SUM/AVG/COUNT/MIN/MAX(column) as alias
    agg_match = re.search(
        r"(SUM|AVG|COUNT|MIN|MAX)\((\w+)\)\s+(?:as|AS)\s+(\w+)",
        query,
        re.IGNORECASE,
    )
    if not agg_match:
        return []

    func, agg_col, alias = agg_match.group(1).upper(), agg_match.group(2), agg_match.group(3)

    pandas_func = {
        "SUM": "sum", "AVG": "mean", "COUNT": "count",
        "MIN": "min", "MAX": "max",
    }[func]

    result = df.groupby(group_col)[agg_col].agg(pandas_func).reset_index()
    result.columns = [group_col, alias]

    # ORDER BY DESC
    if "ORDER BY" in upper and "DESC" in upper:
        result = result.sort_values(alias, ascending=False)

    # LIMIT
    limit = _extract_limit(query)
    if limit:
        result = result.head(limit)

    return result.to_dict(orient="records")


def _extract_limit(query):
    """Extract LIMIT value from SQL query."""
    import re
    match = re.search(r"LIMIT\s+(\d+)", query, re.IGNORECASE)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_warp_session():
    """Patch requests.Session to use mock Warp handlers."""
    with patch("fusion.connectors.warp.requests.Session") as MockSession:
        session = MagicMock()
        session.get.side_effect = _warp_get_handler
        session.post.side_effect = _warp_post_handler
        session.headers = {}
        MockSession.return_value = session
        yield session


@pytest.fixture
def e2e_engine(mock_warp_session):
    """Full stack: real WarpConnector → OLAPEngine (lazy, no data loaded yet)."""
    engine = OLAPEngine(database=":memory:", threads=2, memory_limit="1GB")
    engine.connect_source("ecommerce", {
        "type": "warp",
        "base_url": "http://localhost:8080",
        "database": "ecommerce",
    })
    yield engine
    engine.close()


@pytest.fixture
def e2e_executor(e2e_engine):
    """ToolExecutor wired to full-stack engine."""
    return ToolExecutor(e2e_engine)


@pytest.fixture
def e2e_rest_client(e2e_engine, e2e_executor):
    """FastAPI TestClient wired to full-stack engine."""
    from fastapi.testclient import TestClient
    from fusion.tools.rest_server import create_app

    app = create_app(engine=e2e_engine, executor=e2e_executor)
    return TestClient(app)


# ===========================================================================
# E2E TEST SCENARIOS
# ===========================================================================

class TestE2EConnection:
    """Scenario: User connects to Warp and discovers schema."""

    def test_source_discovered(self, e2e_executor):
        result = e2e_executor.list_sources()
        sources = result["sources"]
        assert len(sources) == 1
        assert sources[0]["source"] == "ecommerce"

    def test_three_tables_discovered(self, e2e_executor):
        result = e2e_executor.list_sources()
        tables = result["sources"][0]["tables"]
        table_names = {t["name"] for t in tables}
        assert table_names == {
            "ecommerce.users",
            "ecommerce.orders",
            "ecommerce.products",
        }

    def test_tables_not_loaded_initially(self, e2e_executor):
        result = e2e_executor.list_sources()
        tables = result["sources"][0]["tables"]
        for t in tables:
            assert t["loaded"] is False

    def test_describe_table_shows_columns(self, e2e_executor):
        result = e2e_executor.describe_table("ecommerce.users")
        col_names = [c["name"] for c in result["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "email" in col_names
        assert "segment" in col_names


class TestE2ELazyLoading:
    """Scenario: Tables load on demand when queried."""

    def test_query_triggers_lazy_load(self, e2e_engine):
        assert not e2e_engine.catalog.is_loaded("ecommerce.users")
        result = e2e_engine.sql("SELECT COUNT(*) as cnt FROM ecommerce.users")
        # After query, table could be pushdown or loaded — either way result is correct
        assert result.row_count == 1

    def test_explicit_load_table(self, e2e_executor):
        result = e2e_executor.load_table("ecommerce.products")
        assert result["status"] == "loaded"

        # Now it should show as loaded
        sources = e2e_executor.list_sources()
        products = next(
            t for t in sources["sources"][0]["tables"]
            if t["name"] == "ecommerce.products"
        )
        assert products["loaded"] is True

    def test_already_loaded_skip(self, e2e_executor):
        e2e_executor.load_table("ecommerce.orders")
        result = e2e_executor.load_table("ecommerce.orders")
        assert result["status"] == "already_loaded"


class TestE2EQueryData:
    """Scenario: User runs analytical SQL queries."""

    def test_simple_select(self, e2e_executor):
        result = e2e_executor.query_data(
            "SELECT * FROM ecommerce.users ORDER BY id"
        )
        assert result["row_count"] == 5
        assert result["rows"][0]["name"] == "Alice"

    def test_aggregation_query(self, e2e_executor):
        """Multi-column aggregation — tables loaded into DuckDB first."""
        # Load tables so DuckDB handles the complex query
        e2e_executor.load_table("ecommerce.orders")

        result = e2e_executor.query_data("""
            SELECT product, COUNT(*) as order_count, SUM(amount) as total
            FROM ecommerce.orders
            GROUP BY product
            ORDER BY total DESC
        """)
        assert result["row_count"] > 0
        first = result["rows"][0]
        assert "product" in first
        assert "total" in first

    def test_join_query(self, e2e_executor):
        """JOIN query — requires both tables in DuckDB."""
        e2e_executor.load_table("ecommerce.users")
        e2e_executor.load_table("ecommerce.orders")

        result = e2e_executor.query_data("""
            SELECT u.name, SUM(o.amount) as total_spent
            FROM ecommerce.users u
            JOIN ecommerce.orders o ON u.id = o.user_id
            GROUP BY u.name
            ORDER BY total_spent DESC
        """)
        assert result["row_count"] > 0
        # Alice has orders: 250 + 120 + 300 = 670
        names = [r["name"] for r in result["rows"]]
        assert "Alice" in names

    def test_guardrails_block_destructive(self, e2e_executor):
        result = e2e_executor.execute("query_data", {"sql": "DROP TABLE ecommerce.users"})
        assert "error" in result

    def test_sql_injection_blocked(self, e2e_executor):
        result = e2e_executor.execute("query_data", {
            "sql": "SELECT * FROM ecommerce.users; DROP TABLE ecommerce.users"
        })
        assert "error" in result


class TestE2ESearchData:
    """Scenario: User searches for specific records."""

    def test_search_exact_match(self, e2e_executor):
        result = e2e_executor.search_data("ecommerce.users", "name", "Alice")
        assert result["row_count"] == 1
        assert result["rows"][0]["email"] == "alice@test.com"

    def test_search_like_pattern(self, e2e_executor):
        result = e2e_executor.search_data("ecommerce.orders", "status", "%compl%")
        assert result["row_count"] >= 1
        for row in result["rows"]:
            assert "compl" in row["status"].lower()

    def test_search_no_match(self, e2e_executor):
        result = e2e_executor.search_data("ecommerce.users", "name", "Nonexistent")
        assert result["row_count"] == 0

    def test_search_with_limit(self, e2e_executor):
        result = e2e_executor.search_data(
            "ecommerce.orders", "status", "completed", limit=2
        )
        assert len(result["rows"]) <= 2


class TestE2EAggregateData:
    """Scenario: User runs GROUP BY aggregations via tool."""

    def test_aggregate_sum(self, e2e_executor):
        result = e2e_executor.aggregate_data(
            "ecommerce.orders", "product", "amount", "SUM"
        )
        assert result["row_count"] > 0
        products = [r["product"] for r in result["rows"]]
        assert "Monitor" in products

    def test_aggregate_count(self, e2e_executor):
        result = e2e_executor.aggregate_data(
            "ecommerce.orders", "status", "id", "COUNT"
        )
        assert result["row_count"] > 0
        statuses = [r["status"] for r in result["rows"]]
        assert "completed" in statuses

    def test_aggregate_invalid_func_blocked(self, e2e_executor):
        result = e2e_executor.aggregate_data(
            "ecommerce.orders", "product", "amount", "EVIL"
        )
        assert "error" in result


class TestE2EMaterializedViews:
    """Scenario: User creates and queries materialized views.

    MV creation uses execute_raw() which runs directly on DuckDB,
    so tables must be loaded first.
    """

    def test_create_view_then_query(self, e2e_executor):
        # Load table first (MV creation bypasses auto-load)
        e2e_executor.load_table("ecommerce.orders")

        result = e2e_executor.create_view(
            "product_totals",
            "SELECT product, SUM(amount) as total FROM ecommerce.orders GROUP BY product",
        )
        assert result["status"] == "created"
        assert result["table_name"] == "mv_product_totals"

        # Query the materialized view
        result = e2e_executor.query_data(
            "SELECT * FROM mv_product_totals ORDER BY total DESC"
        )
        assert result["row_count"] > 0

    def test_list_views(self, e2e_executor):
        e2e_executor.load_table("ecommerce.users")

        e2e_executor.create_view(
            "user_summary",
            "SELECT segment, COUNT(*) as cnt FROM ecommerce.users GROUP BY segment",
        )
        result = e2e_executor.list_views()
        names = [v["name"] for v in result["views"]]
        assert "user_summary" in names

    def test_refresh_view(self, e2e_executor):
        e2e_executor.load_table("ecommerce.orders")

        e2e_executor.create_view(
            "order_stats",
            "SELECT status, COUNT(*) as cnt FROM ecommerce.orders GROUP BY status",
        )
        result = e2e_executor.refresh_view("order_stats")
        assert result["status"] == "refreshed"


class TestE2ECaching:
    """Scenario: Query cache works across repeated queries."""

    def test_second_query_hits_cache(self, e2e_executor):
        sql = "SELECT COUNT(*) as cnt FROM ecommerce.users"
        r1 = e2e_executor.query_data(sql)
        r2 = e2e_executor.query_data(sql)
        assert r1["from_cache"] is False
        assert r2["from_cache"] is True

    def test_cache_stats_reflect_usage(self, e2e_executor):
        e2e_executor.query_data("SELECT COUNT(*) as cnt FROM ecommerce.orders")
        e2e_executor.query_data("SELECT COUNT(*) as cnt FROM ecommerce.orders")
        stats = e2e_executor.cache_stats()
        assert stats["hits"] >= 1


class TestE2EPushdown:
    """Scenario: Pushdown sends queries directly to Warp backend."""

    def test_pushdown_on_unloaded_table(self, e2e_engine, mock_warp_session):
        """Query on unloaded table should attempt pushdown via execute_query."""
        assert not e2e_engine.catalog.is_loaded("ecommerce.users")

        result = e2e_engine.sql("SELECT * FROM ecommerce.users")

        # Should have results (either via pushdown or fallback)
        assert result.row_count == 5

        # Verify POST was called (pushdown route)
        post_calls = [
            call for call in mock_warp_session.post.call_args_list
            if "query/execute" in str(call)
        ]
        assert len(post_calls) > 0, "Expected pushdown POST call to /query/execute"

    def test_no_pushdown_after_load(self, e2e_engine, e2e_executor, mock_warp_session):
        """After explicit load, queries go through DuckDB, not pushdown."""
        e2e_executor.load_table("ecommerce.products")
        mock_warp_session.post.reset_mock()

        result = e2e_engine.sql("SELECT * FROM ecommerce.products")
        assert result.row_count == 7

        # No POST calls (no pushdown) — data was in DuckDB
        pushdown_calls = [
            call for call in mock_warp_session.post.call_args_list
            if "query/execute" in str(call)
        ]
        assert len(pushdown_calls) == 0


class TestE2ERESTAPI:
    """Scenario: User accesses Fusion via HTTP REST API."""

    def test_health(self, e2e_rest_client):
        resp = e2e_rest_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_list_tools(self, e2e_rest_client):
        resp = e2e_rest_client.get("/tools")
        assert resp.status_code == 200
        assert resp.json()["count"] == 10

    def test_get_sources(self, e2e_rest_client):
        resp = e2e_rest_client.get("/sources")
        assert resp.status_code == 200
        assert resp.json()["sources"][0]["source"] == "ecommerce"

    def test_get_table_schema(self, e2e_rest_client):
        resp = e2e_rest_client.get("/tables/ecommerce.users/schema")
        assert resp.status_code == 200
        cols = resp.json()["columns"]
        col_names = [c["name"] for c in cols]
        assert "name" in col_names

    def test_query_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.post("/query", json={
            "sql": "SELECT * FROM ecommerce.users ORDER BY id LIMIT 5"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["row_count"] == 5

    def test_search_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.post("/search", json={
            "table": "ecommerce.users",
            "filter_column": "name",
            "filter_value": "Bob",
        })
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1

    def test_aggregate_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.post("/aggregate", json={
            "table": "ecommerce.orders",
            "group_by": "product",
            "agg_column": "amount",
            "agg_func": "SUM",
        })
        assert resp.status_code == 200
        assert resp.json()["row_count"] > 0

    def test_tool_dispatch_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.post("/tools/cache_stats", json={})
        assert resp.status_code == 200

    def test_guardrail_403(self, e2e_rest_client):
        resp = e2e_rest_client.post("/query", json={
            "sql": "DROP TABLE ecommerce.users"
        })
        assert resp.status_code == 403

    def test_create_view_via_rest(self, e2e_rest_client):
        # Load table first (MV creation uses execute_raw)
        e2e_rest_client.post("/tables/ecommerce.orders/load")

        resp = e2e_rest_client.post("/views", json={
            "name": "rest_test_view",
            "sql": "SELECT product, COUNT(*) as cnt FROM ecommerce.orders GROUP BY product",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    def test_views_list_via_rest(self, e2e_rest_client):
        # Load table first, then create a view
        e2e_rest_client.post("/tables/ecommerce.users/load")
        e2e_rest_client.post("/views", json={
            "name": "rest_list_view",
            "sql": "SELECT COUNT(*) as cnt FROM ecommerce.users",
        })
        resp = e2e_rest_client.get("/views")
        assert resp.status_code == 200
        assert len(resp.json()["views"]) >= 1

    def test_load_table_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.post("/tables/ecommerce.products/load")
        assert resp.status_code == 200
        assert resp.json()["status"] == "loaded"

    def test_cache_stats_via_rest(self, e2e_rest_client):
        resp = e2e_rest_client.get("/cache/stats")
        assert resp.status_code == 200


class TestE2EMultiTableWorkflow:
    """Scenario: Realistic multi-step analytics workflow."""

    def test_full_analytics_workflow(self, e2e_executor):
        """Simulates a real user session: discover → explore → load → analyze → cache."""
        # Step 1: Discover what's available
        sources = e2e_executor.list_sources()
        assert len(sources["sources"]) == 1
        table_names = [t["name"] for t in sources["sources"][0]["tables"]]
        assert "ecommerce.orders" in table_names

        # Step 2: Explore schema
        schema = e2e_executor.describe_table("ecommerce.orders")
        col_names = [c["name"] for c in schema["columns"]]
        assert "amount" in col_names
        assert "product" in col_names

        # Step 3: Run exploratory query (pushdown or auto-load)
        result = e2e_executor.query_data(
            "SELECT * FROM ecommerce.orders ORDER BY amount DESC LIMIT 3"
        )
        assert result["row_count"] == 3

        # Step 4: Load table explicitly for DuckDB-local operations
        e2e_executor.load_table("ecommerce.orders")

        # Step 5: Aggregate analysis
        agg = e2e_executor.aggregate_data(
            "ecommerce.orders", "product", "amount", "SUM"
        )
        assert agg["row_count"] > 0

        # Step 6: Create materialized view for repeated use
        e2e_executor.create_view(
            "revenue_by_product",
            "SELECT product, SUM(amount) as revenue FROM ecommerce.orders GROUP BY product",
        )

        # Step 7: Query the materialized view
        mv_result = e2e_executor.query_data(
            "SELECT * FROM mv_revenue_by_product ORDER BY revenue DESC"
        )
        assert mv_result["row_count"] > 0

        # Step 8: Check cache
        stats = e2e_executor.cache_stats()
        assert stats["cached_queries"] > 0

    def test_cross_table_join_workflow(self, e2e_executor):
        """Join users and orders to find top customers — requires loaded tables."""
        e2e_executor.load_table("ecommerce.users")
        e2e_executor.load_table("ecommerce.orders")

        result = e2e_executor.query_data("""
            SELECT u.name, u.segment, COUNT(o.id) as order_count, SUM(o.amount) as total
            FROM ecommerce.users u
            JOIN ecommerce.orders o ON u.id = o.user_id
            GROUP BY u.name, u.segment
            ORDER BY total DESC
        """)
        assert result["row_count"] > 0
        top = result["rows"][0]
        assert "name" in top
        assert "total" in top
        assert "segment" in top
