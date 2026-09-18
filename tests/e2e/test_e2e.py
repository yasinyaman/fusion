"""End-to-end scenarios: mock Warp HTTP -> WarpSource -> FusionApp -> tools / REST.

The ``e2e_app`` fixture wires a real ``WarpSource`` to ``FakeWarpTransport``,
so pagination, schema inference, pushdown and lazy loading all run for real.
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fusion.adapters.inbound.rest.app import create_app
from fusion.domain.errors import QueryError
from tests.data import MOCK_DB
from tests.fakes import FakeWarpTransport


@pytest.fixture
def tools(e2e_app):
    return e2e_app.tools


@pytest.fixture
def client(e2e_app):
    return TestClient(create_app(e2e_app))


def _pushdown_calls(transport):
    return [u for u in transport.urls("POST") if "query/execute" in u]


class TestConnection:
    def test_source_and_tables_discovered(self, tools):
        result = tools.list_sources()
        assert [s["source"] for s in result["sources"]] == ["ecommerce"]
        tables = result["sources"][0]["tables"]
        assert {t["name"] for t in tables} == {
            "ecommerce.users",
            "ecommerce.orders",
            "ecommerce.products",
        }
        assert all(t["loaded"] is False for t in tables)

    def test_describe_table_shows_columns(self, tools):
        cols = {c["name"]: c for c in tools.describe_table("ecommerce.users")["columns"]}
        assert set(cols) == {"id", "name", "email", "segment"}
        assert cols["id"]["type"] == "integer"


class TestLazyLoading:
    def test_query_on_unloaded_table(self, e2e_app):
        assert not e2e_app.catalog.is_loaded("ecommerce.users")
        assert e2e_app.query.sql("SELECT COUNT(*) AS cnt FROM ecommerce.users").row_count == 1

    def test_explicit_load_table(self, tools):
        assert tools.load_table("ecommerce.products")["status"] == "loaded"
        tables = {t["name"]: t for t in tools.list_sources()["sources"][0]["tables"]}
        assert tables["ecommerce.products"]["loaded"] is True
        assert tables["ecommerce.products"]["row_count"] == 7
        assert tools.load_table("ecommerce.products")["status"] == "already_loaded"


class TestQueryData:
    def test_simple_select(self, tools):
        result = tools.query_data("SELECT * FROM ecommerce.users ORDER BY id")
        assert result["row_count"] == 5
        assert result["rows"][0]["name"] == "Alice"

    def test_aggregation_query(self, tools):
        tools.load_table("ecommerce.orders")
        result = tools.query_data(
            "SELECT product, COUNT(*) AS order_count, SUM(amount) AS total "
            "FROM ecommerce.orders GROUP BY product ORDER BY total DESC"
        )
        assert result["rows"][0]["product"] == "Monitor"

    def test_join_query_auto_loads_both_tables(self, e2e_app, tools):
        result = tools.query_data(
            "SELECT u.name, SUM(o.amount) AS total_spent FROM ecommerce.users u "
            "JOIN ecommerce.orders o ON u.id = o.user_id GROUP BY u.name ORDER BY total_spent DESC"
        )
        assert result["rows"][0] == {"name": "Charlie", "total_spent": 860.0}
        assert e2e_app.catalog.is_loaded("ecommerce.users")
        assert e2e_app.catalog.is_loaded("ecommerce.orders")

    def test_guardrails_block_destructive_and_injection(self, tools):
        assert "error" in tools.execute("query_data", {"sql": "DROP TABLE ecommerce.users"})
        assert "error" in tools.execute(
            "query_data", {"sql": "SELECT * FROM ecommerce.users; DROP TABLE ecommerce.users"}
        )


class TestSearchData:
    def test_exact_match_via_pushdown(self, tools, e2e_transport):
        result = tools.search_data("ecommerce.users", "name", "Alice")
        assert result["row_count"] == 1
        assert result["rows"][0]["email"] == "alice@test.com"
        assert _pushdown_calls(e2e_transport)

    def test_like_pattern(self, tools):
        result = tools.search_data("ecommerce.orders", "status", "%compl%")
        assert result["row_count"] == 5
        assert all("compl" in r["status"] for r in result["rows"])

    def test_no_match_and_limit(self, tools):
        assert tools.search_data("ecommerce.users", "name", "Nonexistent")["row_count"] == 0
        assert (
            len(tools.search_data("ecommerce.orders", "status", "completed", limit=2)["rows"]) <= 2
        )


class TestAggregateData:
    def test_aggregate_sum_via_pushdown(self, tools, e2e_transport):
        result = tools.aggregate_data("ecommerce.orders", "product", "amount", "SUM")
        assert result["rows"][0] == {"product": "Monitor", "sum_amount": 800.0}
        assert _pushdown_calls(e2e_transport)

    def test_aggregate_count(self, tools):
        result = tools.aggregate_data("ecommerce.orders", "status", "id", "COUNT")
        assert {r["status"] for r in result["rows"]} == {"completed", "pending", "cancelled"}

    def test_aggregate_invalid_func_blocked(self, tools):
        assert "error" in tools.aggregate_data("ecommerce.orders", "product", "amount", "EVIL")


class TestMaterializedViews:
    def test_create_view_without_loading_first(self, tools, e2e_app):
        result = tools.create_view(
            "product_totals",
            "SELECT product, SUM(amount) AS total FROM ecommerce.orders GROUP BY product",
        )
        assert result["table_name"] == "mv_product_totals"
        assert e2e_app.catalog.is_loaded("ecommerce.orders")
        rows = tools.query_data("SELECT * FROM mv_product_totals ORDER BY total DESC")["rows"]
        assert rows[0]["product"] == "Monitor"

    def test_list_and_refresh(self, tools):
        tools.create_view(
            "user_summary", "SELECT segment, COUNT(*) AS cnt FROM ecommerce.users GROUP BY segment"
        )
        assert [v["name"] for v in tools.list_views()["views"]] == ["user_summary"]
        assert tools.refresh_view("user_summary")["status"] == "refreshed"


class TestCaching:
    def test_second_query_hits_cache(self, tools):
        sql = "SELECT COUNT(*) AS cnt FROM ecommerce.users"
        assert tools.query_data(sql)["from_cache"] is False
        assert tools.query_data(sql)["from_cache"] is True
        assert tools.cache_stats()["hits"] >= 1


class TestPushdown:
    def test_pushdown_on_unloaded_table(self, e2e_app, e2e_transport):
        result = e2e_app.query.sql("SELECT * FROM ecommerce.users")
        assert result.row_count == 5
        assert _pushdown_calls(e2e_transport)
        assert not e2e_app.catalog.is_loaded("ecommerce.users")

    def test_no_pushdown_after_load(self, e2e_app, tools, e2e_transport):
        tools.load_table("ecommerce.products")
        e2e_transport.requests.clear()
        assert e2e_app.query.sql("SELECT * FROM ecommerce.products").row_count == 7
        assert _pushdown_calls(e2e_transport) == []

    def test_pushdown_failure_falls_back_to_local(self, e2e_app, e2e_transport):
        e2e_transport.fail_next("HTTP 500", status=500)
        result = e2e_app.query.sql("SELECT COUNT(*) AS c FROM ecommerce.orders")
        assert result.rows == [(8,)]
        assert e2e_app.catalog.is_loaded("ecommerce.orders")


class TestLegacyWarp:
    """Fusion keeps working against a Warp 0.9 (no schema/export, raw query off)."""

    def test_queries_load_tables_without_pushdown(self, legacy_app, legacy_transport):
        result = legacy_app.query.sql("SELECT COUNT(*) AS cnt FROM ecommerce.orders")
        assert result.rows == [(8,)]
        assert _pushdown_calls(legacy_transport) == []  # /info said raw_query_enabled=false
        assert legacy_app.catalog.is_loaded("ecommerce.orders")
        # The db-scoped URL 404s on a single-database Warp 0.9, so the source
        # switched to the un-prefixed layout and stayed there.
        legacy_transport.requests.clear()
        legacy_app.query.sql("SELECT COUNT(*) AS cnt FROM ecommerce.products")
        assert legacy_transport.urls("GET")
        assert all("/ecommerce/" not in u for u in legacy_transport.urls("GET"))

    def test_tools_work_end_to_end(self, legacy_app):
        tools = legacy_app.tools
        assert tools.search_data("ecommerce.users", "name", "Alice")["row_count"] == 1
        agg = tools.aggregate_data("ecommerce.orders", "product", "amount", "SUM")
        assert agg["rows"][0] == {"product": "Monitor", "sum_amount": 800.0}
        assert tools.list_sources()["sources"][0]["source"] == "ecommerce"

    def test_403_is_learned_once(self, denying_app, denying_transport):
        # Warp advertises raw_query but refuses it at runtime (production).
        assert denying_app.query.sql("SELECT COUNT(*) AS c FROM ecommerce.users").rows == [(5,)]
        assert len(_pushdown_calls(denying_transport)) == 1  # the single 403
        assert denying_app.query.sql("SELECT COUNT(*) AS c FROM ecommerce.orders").rows == [(8,)]
        assert len(_pushdown_calls(denying_transport)) == 1  # never retried
        source = denying_app.sources.source("ecommerce")
        assert source is not None and source.supports_pushdown is False


class TestRestApi:
    def test_health_and_tools(self, client):
        assert client.get("/health").json()["status"] == "healthy"
        assert client.get("/tools").json()["count"] == 10
        assert client.get("/readiness").status_code == 200

    def test_sources_and_schema(self, client):
        assert client.get("/sources").json()["sources"][0]["source"] == "ecommerce"
        cols = client.get("/tables/ecommerce.users/schema").json()["columns"]
        assert "name" in [c["name"] for c in cols]

    def test_query_search_aggregate(self, client):
        resp = client.post(
            "/query", json={"sql": "SELECT * FROM ecommerce.users ORDER BY id LIMIT 5"}
        )
        assert resp.json()["row_count"] == 5
        resp = client.post(
            "/search",
            json={"table": "ecommerce.users", "filter_column": "name", "filter_value": "Bob"},
        )
        assert resp.json()["row_count"] == 1
        resp = client.post(
            "/aggregate",
            json={
                "table": "ecommerce.orders",
                "group_by": "product",
                "agg_column": "amount",
                "agg_func": "SUM",
            },
        )
        assert resp.json()["row_count"] > 0

    def test_tool_dispatch_and_guardrail(self, client):
        assert client.post("/tools/cache_stats", json={}).status_code == 200
        assert client.post("/query", json={"sql": "DROP TABLE ecommerce.users"}).status_code == 403

    def test_views_and_load(self, client):
        resp = client.post(
            "/views",
            json={
                "name": "rest_view",
                "sql": "SELECT product, COUNT(*) AS cnt FROM ecommerce.orders GROUP BY product",
            },
        )
        assert resp.json()["status"] == "created"
        assert len(client.get("/views").json()["views"]) == 1
        assert client.post("/tables/ecommerce.products/load").json()["status"] == "loaded"
        assert client.get("/cache/stats").status_code == 200


class TestWorkflows:
    def test_full_analytics_workflow(self, tools):
        sources = tools.list_sources()
        assert "ecommerce.orders" in [t["name"] for t in sources["sources"][0]["tables"]]
        schema = tools.describe_table("ecommerce.orders")
        assert {"amount", "product"} <= {c["name"] for c in schema["columns"]}
        assert (
            tools.query_data("SELECT * FROM ecommerce.orders ORDER BY amount DESC LIMIT 3")[
                "row_count"
            ]
            == 3
        )
        tools.load_table("ecommerce.orders")
        assert tools.aggregate_data("ecommerce.orders", "product", "amount", "SUM")["row_count"] > 0
        tools.create_view(
            "revenue_by_product",
            "SELECT product, SUM(amount) AS revenue FROM ecommerce.orders GROUP BY product",
        )
        mv = tools.query_data("SELECT * FROM mv_revenue_by_product ORDER BY revenue DESC")
        assert mv["rows"][0]["product"] == "Monitor"
        assert tools.cache_stats()["cached_queries"] > 0

    def test_cross_table_join_workflow(self, tools):
        result = tools.query_data(
            "SELECT u.name, u.segment, COUNT(o.id) AS order_count, SUM(o.amount) AS total "
            "FROM ecommerce.users u JOIN ecommerce.orders o ON u.id = o.user_id "
            "GROUP BY u.name, u.segment ORDER BY total DESC"
        )
        assert result["rows"][0]["name"] == "Charlie"
        assert result["rows"][0]["order_count"] == 2


class TestSmartTransfer:
    """A table too big for memory is still queryable, one slice at a time."""

    def _exports(self, transport):
        return [(m, u, p) for m, u, p in transport.requests if u.endswith("/export")]

    def test_a_filtered_query_fetches_only_the_matching_rows(self, big_app, big_transport):
        result = big_app.query.sql(
            "SELECT id, amount FROM ecommerce.orders WHERE status = 'pending'"
        )
        assert result.row_count == 2
        assert not big_app.catalog.is_loaded("ecommerce.orders")
        slices = big_app.catalog.slices_of("ecommerce.orders")
        assert len(slices) == 1 and slices[0].row_count == 2
        params = self._exports(big_transport)[-1][2]
        assert params["filter[status][eq]"] == "pending"
        assert set(params["fields"].split(",")) == {"amount", "id", "status"}

    def test_list_sources_shows_the_estimate_and_the_slices(self, big_app):
        big_app.query.sql("SELECT id FROM ecommerce.orders WHERE status = 'pending'")
        tables = {t["name"]: t for t in big_app.tools.list_sources()["sources"][0]["tables"]}
        orders = tables["ecommerce.orders"]
        assert orders["loaded"] is False
        assert big_app.catalog.get_table("ecommerce.orders").row_estimate == 9_000_000

    def test_an_unfiltered_query_is_refused_with_advice(self, big_app, big_transport):
        with pytest.raises(QueryError) as error:
            big_app.query.sql("SELECT * FROM ecommerce.orders")
        message = str(error.value)
        assert "9,000,000 rows estimated" in message
        assert "add a WHERE condition" in message
        assert self._exports(big_transport) == []  # nothing was fetched

    def test_the_slice_is_reused_by_a_narrower_query(self, big_app, big_transport):
        big_app.query.sql("SELECT * FROM ecommerce.orders WHERE amount > 50")
        before = len(self._exports(big_transport))
        result = big_app.query.sql("SELECT * FROM ecommerce.orders WHERE amount > 200")
        assert result.row_count == 3
        assert len(self._exports(big_transport)) == before

    def test_a_small_table_still_loads_whole_and_joins(self, big_app):
        result = big_app.query.sql(
            "SELECT u.name, o.amount FROM ecommerce.users u "
            "JOIN ecommerce.orders o ON u.id = o.user_id "
            "WHERE o.status = 'pending' ORDER BY o.amount"
        )
        assert [r["name"] for r in result.to_records()] == ["Charlie", "Alice"]
        assert big_app.catalog.is_loaded("ecommerce.users")
        assert not big_app.catalog.is_loaded("ecommerce.orders")

    def test_rows_arrive_over_arrow(self, big_app, big_transport):
        big_app.query.sql("SELECT amount FROM ecommerce.orders WHERE status = 'completed'")
        assert self._exports(big_transport)[-1][2]["format"] == "arrow"
        rows = big_app.query.sql(
            "SELECT SUM(amount) AS total FROM ecommerce.orders WHERE status = 'completed'"
        )
        assert rows.rows == [(1415.0,)]

    def test_legacy_warp_slices_through_the_list_endpoint(self, settings, scheduler):
        from tests.conftest import _e2e_app

        transport = FakeWarpTransport(
            MOCK_DB,
            database="ecommerce",
            mode="legacy",
            raw_query=False,
            page_format="data",
            row_estimates={"orders": 9_000_000},
        )
        app = _e2e_app(
            replace(settings, full_load_max_rows=100, slice_max_rows=1000), scheduler, transport
        )
        try:
            result = app.query.sql("SELECT id FROM ecommerce.orders WHERE status = 'pending'")
            assert result.row_count == 2
            assert self._exports(transport) == []  # no export endpoint on 0.9
            filtered = [p for m, u, p in transport.requests if "filter[status][eq]" in p]
            assert filtered and all(p["filter[status][eq]"] == "pending" for p in filtered)
        finally:
            app.close()

    def test_pushdown_still_wins_when_warp_allows_raw_sql(self, settings, scheduler):
        from tests.conftest import _e2e_app

        transport = FakeWarpTransport(
            MOCK_DB, database="ecommerce", row_estimates={"orders": 9_000_000}
        )
        app = _e2e_app(
            replace(settings, full_load_max_rows=100, slice_max_rows=1000), scheduler, transport
        )
        try:
            # Warp answers the whole query itself, so nothing is materialized
            # and the size limits never come into play.
            assert app.query.sql("SELECT * FROM ecommerce.orders").row_count == 8
            assert app.catalog.slices_of("ecommerce.orders") == []
            assert _pushdown_calls(transport)
        finally:
            app.close()


class TestSemiJoinOverWarp:
    """A join to a huge Warp table fetches only the rows that can match."""

    def test_keys_are_passed_to_warp(self, big_app, big_transport):
        result = big_app.query.sql(
            "SELECT u.name, COUNT(*) AS n FROM ecommerce.users u "
            "JOIN ecommerce.orders o ON u.id = o.user_id "
            "GROUP BY u.name ORDER BY n DESC, u.name"
        )
        assert result.to_records()[0] == {"name": "Alice", "n": 3}
        assert big_app.catalog.is_loaded("ecommerce.users")
        assert not big_app.catalog.is_loaded("ecommerce.orders")
        loaded = big_app.catalog.slices_of("ecommerce.orders")[0]
        assert loaded.derived_from == "semijoin:ecommerce.users.id"
        exported = [p for m, u, p in big_transport.requests if u.endswith("/orders/export")]
        assert exported, "the big table was never exported"
        assert "filter[user_id][in]" in exported[-1]


class TestIncrementalRefreshOverWarp:
    """A watermarked table is topped up through the export endpoint."""

    def test_only_new_rows_are_exported(self, settings, scheduler):
        from tests.conftest import _e2e_app

        transport = FakeWarpTransport(
            {**MOCK_DB, "events": [{"id": 1, "kind": "a"}, {"id": 2, "kind": "b"}]},
            database="ecommerce",
            raw_query=False,
        )
        app = _e2e_app(
            replace(
                settings,
                refresh_config='{"ecommerce.events": {"watermark_column": "id", '
                '"key_columns": ["id"]}}',
            ),
            scheduler,
            transport,
        )
        try:
            app.sources.ensure_loaded(["ecommerce.events"])
            assert app.query.sql("SELECT COUNT(*) AS n FROM ecommerce.events").rows == [(2,)]
            transport.db["events"].append({"id": 3, "kind": "c"})
            app.sources.refresh_all()
            exported = [p for m, u, p in transport.requests if u.endswith("/events/export")]
            assert exported[-1]["filter[id][gt]"] == "2"
            result = app.query.sql("SELECT COUNT(*) AS n FROM ecommerce.events")
            assert result.rows == [(3,)] and result.from_cache is False
        finally:
            app.close()
