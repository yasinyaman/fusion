"""Tests for WarpSource over the FakeWarpTransport."""

import pytest

from fusion.adapters.outbound.warp.http import HttpTransportError, WarpHttpClient
from fusion.adapters.outbound.warp.source import (
    WarpSource,
    build_filter_sql,
    extract_row_count,
    extract_rows,
    extract_tables,
    filter_rowset,
    infer_column_type,
)
from fusion.domain.errors import ConnectionError, QueryError
from fusion.domain.models import RowSet
from fusion.domain.slices import Predicate, SliceSpec
from fusion.ports.data_source import DataSource, PushdownCapable
from tests.fakes import FakeWarpTransport

USERS = [
    {"id": 1, "name": "Alice", "score": 95.5, "active": True},
    {"id": 2, "name": "Bob", "score": None, "active": False},
    {"id": 3, "name": "Cara", "score": 70.0, "active": True},
]
ORDERS = [{"id": i, "user_id": (i % 3) + 1, "amount": 10.0 * i} for i in range(1, 6)]
PRED_ACTIVE = Predicate("active", "eq", True)


def _source(page_size=1000, **transport_kwargs):
    transport = FakeWarpTransport({"users": USERS, "orders": ORDERS}, "db", **transport_kwargs)
    source = WarpSource.from_config(
        "db",
        {"base_url": "http://localhost:8080", "transport": transport, "page_size": page_size},
    )
    return source, transport


class TestConnect:
    def test_satisfies_ports(self):
        source, _ = _source()
        assert isinstance(source, DataSource)
        assert isinstance(source, PushdownCapable)
        assert source.supports_pushdown is True
        assert source.source_type == "warp"

    def test_connect_discovers_tables(self):
        source, transport = _source()
        assert not source.is_connected
        source.connect()
        assert source.is_connected
        assert source.tables == ["users", "orders"]
        assert transport.urls("GET") == [
            "http://localhost:8080/health",
            "http://localhost:8080/info",
        ]

    def test_connect_health_failure(self):
        source, _ = _source(health_ok=False)
        with pytest.raises(ConnectionError, match="Cannot connect"):
            source.connect()

    def test_connect_info_failure(self):
        source, transport = _source()
        original = transport.get_json

        def flaky(url, params=None, timeout=None):
            if url.endswith("/info"):
                raise HttpTransportError("HTTP 500", status=500)
            return original(url, params, timeout)

        transport.get_json = flaky  # type: ignore[method-assign]
        with pytest.raises(ConnectionError, match="discover tables"):
            source.connect()

    def test_close(self):
        source, transport = _source()
        source.connect()
        source.close()
        assert not source.is_connected
        assert transport.closed

    def test_not_connected_guards(self):
        source, _ = _source()
        with pytest.raises(ConnectionError, match="Not connected"):
            source.fetch_table("users")
        with pytest.raises(ConnectionError, match="Not connected"):
            source.discover_schema()
        with pytest.raises(ConnectionError, match="Not connected"):
            source.execute_query("SELECT 1")


class TestFetch:
    def test_fetch_table(self):
        source, _ = _source()
        source.connect()
        rows = source.fetch_table("users")
        assert rows.columns == ("id", "name", "score", "active")
        assert len(rows) == 3
        assert rows.rows[0] == (1, "Alice", 95.5, True)

    def test_fetch_table_uses_the_export_endpoint(self):
        source, transport = _source()
        source.connect()
        source.fetch_table("orders")
        assert transport.urls("GET")[-1].endswith("/orders/export")
        assert transport.requests[-1][2]["format"] == "arrow"

    def test_fetch_table_data_wrapper_format(self):
        source, _ = _source(mode="legacy", page_format="data")
        source.connect()
        assert len(source.fetch_table("orders")) == 5

    def test_pagination_without_an_export_endpoint(self):
        source, transport = _source(page_size=2, mode="legacy")
        source.connect()
        rows = source.fetch_table("orders")
        assert [r[0] for r in rows.rows] == [1, 2, 3, 4, 5]
        pages = [p for m, u, p in transport.requests if u.endswith("/orders")]
        assert pages == [
            {"limit": 2, "offset": 0},
            {"limit": 2, "offset": 2},
            {"limit": 2, "offset": 4},
        ]

    def test_max_rows_truncates_and_never_overfetches(self):
        source, transport = _source(page_size=2, mode="legacy")
        source.connect()
        rows = source.fetch_table("orders", max_rows=3)
        assert len(rows) == 3
        pages = [p for m, u, p in transport.requests if u.endswith("/orders")]
        assert pages == [{"limit": 2, "offset": 0}, {"limit": 1, "offset": 2}]

    def test_max_rows_is_passed_to_the_export(self):
        source, transport = _source()
        source.connect()
        assert len(source.fetch_table("orders", max_rows=2)) == 2
        assert transport.requests[-1][2]["limit"] == 2

    def test_unknown_table_raises_query_error(self):
        source, _ = _source()
        source.connect()
        with pytest.raises(QueryError, match="Failed to fetch"):
            source.fetch_table("nope")

    def test_unknown_table_does_not_disable_exports(self):
        source, transport = _source()
        source.connect()
        with pytest.raises(QueryError):
            source.fetch_table("nope")
        source.fetch_table("users")
        assert transport.urls("GET")[-1].endswith("/users/export")

    def test_empty_table(self):
        transport = FakeWarpTransport({"empty": []}, "db")
        source = WarpSource.from_config("db", {"transport": transport})
        source.connect()
        assert source.fetch_table("empty").is_empty


class TestSchema:
    def test_typed_schema_from_the_schema_endpoint(self):
        source, transport = _source(row_estimates={"orders": 4_000_000})
        source.connect()
        schema = source.discover_schema()
        users = {c.name: c for c in schema["users"].columns}
        assert users["id"].type == "integer"
        assert users["name"].type == "varchar"
        assert users["score"].type == "double"
        assert users["score"].nullable is True
        assert users["active"].type == "boolean"
        assert users["active"].nullable is False
        # The estimate comes from the source; nothing is loaded yet.
        assert schema["orders"].row_estimate == 4_000_000
        assert schema["orders"].row_count == -1
        assert transport.urls("GET")[-1].endswith("/api/v1/db/schema")

    def test_falls_back_to_sampling_when_the_schema_endpoint_fails(self):
        source, transport = _source()
        source.connect()
        transport.fail_next("HTTP 500", status=500)
        schema = source.discover_schema()
        assert {c.name for c in schema["users"].columns} == {"id", "name", "score", "active"}
        assert [p for m, u, p in transport.requests if u.endswith("/users")][-1]["limit"] == 5

    def test_sampling_infers_types_and_row_counts(self):
        source, transport = _source(mode="legacy", page_format="data")
        source.connect()
        schema = source.discover_schema()
        users = {c.name: c for c in schema["users"].columns}
        assert users["id"].type == "integer" and users["score"].type == "double"
        assert schema["orders"].row_count == 5
        assert schema["orders"].row_estimate == 5
        pages = [p for m, u, p in transport.requests if "/api/v1/" in u]
        assert all(p["limit"] == 5 for p in pages)

    def test_sampling_without_a_total_leaves_the_count_unknown(self):
        source, _ = _source(mode="legacy", page_format="list")
        source.connect()
        assert source.discover_schema()["orders"].row_count == -1

    def test_discover_schema_tolerates_failures(self):
        transport = FakeWarpTransport(
            {"users": USERS}, "db", mode="legacy", info={"tables": ["users", "ghost"]}
        )
        source = WarpSource.from_config("db", {"transport": transport})
        source.connect()
        schema = source.discover_schema()
        assert schema["ghost"].columns == []
        assert schema["users"].columns


class TestPushdown:
    def test_execute_query(self):
        source, transport = _source()
        source.connect()
        rows = source.execute_query("SELECT COUNT(*) AS count FROM users")
        assert rows.to_records() == [{"count": 3}]
        assert transport.requests[-1][1].endswith("/api/v1/db/query/execute")

    def test_execute_query_failure(self):
        source, transport = _source()
        source.connect()
        transport.fail_next("HTTP 500", status=500)
        with pytest.raises(QueryError, match="Warp query execution failed"):
            source.execute_query("SELECT 1")

    def test_fetch_filtered_pushes_sql(self):
        source, transport = _source()
        source.connect()
        rows = source.fetch_filtered("users", {"name": "Alice"}, limit=5)
        assert rows.to_records() == [USERS[0]]
        assert (
            transport.requests[-1][2]["query"] == "SELECT * FROM users WHERE name = 'Alice' LIMIT 5"
        )

    def test_fetch_filtered_falls_back_to_full_fetch(self):
        source, transport = _source()
        source.connect()
        transport.fail_next("HTTP 500", status=500)
        rows = source.fetch_filtered("users", {"active": True}, columns=["id"], limit=1)
        assert rows.to_records() == [{"id": 1}]


class TestCapabilities:
    def test_defaults_before_connect(self):
        source, _ = _source()
        caps = source.warp_capabilities
        assert caps.legacy and caps.raw_query and not caps.schema and not caps.export

    def test_reads_0_10_capabilities(self):
        source, transport = _source(export_max_rows=100)
        source.connect()
        caps = source.warp_capabilities
        assert caps.version == "0.10.0"
        assert not caps.legacy
        assert caps.schema and caps.export and caps.arrow
        assert caps.export_formats == ("json", "ndjson", "arrow")
        assert caps.export_max_rows == 100
        assert caps.raw_query is True
        assert "in" in caps.filter_ops
        assert source.supports_pushdown is True
        assert transport.urls("GET") == [
            "http://localhost:8080/health",
            "http://localhost:8080/info",
        ]  # no layout probe on a 0.10 Warp

    def test_arrow_only_when_advertised(self):
        source, _ = _source(arrow=False)
        source.connect()
        assert source.warp_capabilities.export and not source.warp_capabilities.arrow

    def test_legacy_info_uses_settings(self):
        source, transport = _source(mode="legacy", raw_query=False)
        source.connect()
        caps = source.warp_capabilities
        assert caps.legacy and caps.version == "0.9.0"
        assert not caps.schema and not caps.export
        assert caps.raw_query is False
        assert source.supports_pushdown is False
        # Connecting costs exactly two requests; the layout is only probed
        # if a db-scoped URL actually 404s.
        assert transport.urls("GET") == [
            "http://localhost:8080/health",
            "http://localhost:8080/info",
        ]
        assert source._client.db_prefixed is True

    def test_legacy_single_db_falls_back_to_unprefixed_routes(self):
        source, transport = _source(mode="legacy", single_db_unprefixed=True)
        source.connect()
        assert source._client.db_prefixed is True  # nothing proved otherwise yet
        assert source.fetch_table("users").column("id") == [1, 2, 3]
        assert source._client.db_prefixed is False
        assert transport.urls("GET")[2] == "http://localhost:8080/api/v1/db/users"  # the 404
        assert transport.urls("GET")[3] == "http://localhost:8080/api/v1/users"
        # The fallback is sticky: later fetches go straight to the right layout.
        transport.requests.clear()
        source.fetch_table("orders")
        assert all("/db/" not in u for u in transport.urls("GET"))

    def test_unknown_table_keeps_the_prefixed_layout(self):
        source, _ = _source(mode="legacy")
        source.connect()
        with pytest.raises(QueryError):
            source.fetch_table("ghost")
        assert source._client.db_prefixed is True

    def test_no_layout_fallback_on_a_0_10_warp(self):
        source, transport = _source(single_db_unprefixed=True)  # 0.10 mode
        source.connect()
        with pytest.raises(QueryError):
            source.fetch_table("users")  # 404 is taken at face value
        assert source._client.db_prefixed is True

    def test_custom_api_prefix(self):
        source, transport = _source(api_prefix="/v2")
        source.connect()
        source.fetch_table("users")
        assert transport.requests[-1][1] == "http://localhost:8080/v2/db/users/export"

    def test_403_disables_pushdown(self):
        source, transport = _source(deny_raw_query=True)
        source.connect()
        assert source.supports_pushdown is True
        with pytest.raises(QueryError, match="403"):
            source.execute_query("SELECT 1")
        assert source.supports_pushdown is False
        assert source.warp_capabilities.raw_query is True  # /info was not wrong, just refused

    def test_other_errors_keep_pushdown(self):
        source, transport = _source()
        source.connect()
        transport.fail_next("HTTP 500", status=500)
        with pytest.raises(QueryError):
            source.execute_query("SELECT 1")
        assert source.supports_pushdown is True

    def test_from_info_tolerates_garbage(self):
        from fusion.adapters.outbound.warp.capabilities import from_info

        assert from_info(None).legacy
        assert from_info({"capabilities": "nope", "settings": {"api_prefix": "/x"}}).api_prefix == (
            "/x"
        )
        caps = from_info({"capabilities": {"export": {"formats": "arrow"}}})
        assert caps.export_formats == () and caps.db_prefix == "always"
        assert caps.as_dict()["arrow"] is False


class TestHelpers:
    def test_extract_tables_formats(self):
        assert extract_tables({"tables": ["t1", "t2"]}, "db") == ["t1", "t2"]
        assert extract_tables({"tables": [{"name": "t1"}, {"name": "t2"}]}, "db") == ["t1", "t2"]
        assert extract_tables(["t1", "t2"], "db") == ["t1", "t2"]
        list_fmt = {
            "databases": [
                {"name": "db", "tables": ["users", "orders"]},
                {"name": "other", "tables": ["logs"]},
            ]
        }
        assert extract_tables(list_fmt, "db") == ["users", "orders"]
        dict_fmt = {
            "databases": {
                "db": {"tables": ["categories", "orders"], "table_count": 2},
                "other_db": {"tables": ["logs"], "table_count": 1},
            }
        }
        assert extract_tables(dict_fmt, "db") == ["categories", "orders"]
        assert extract_tables(dict_fmt, "") == ["categories", "orders", "logs"]
        assert extract_tables({"unexpected": 1}, "db") == []

    def test_extract_rows_and_count(self):
        assert extract_rows([{"a": 1}]) == [{"a": 1}]
        assert extract_rows({"rows": [{"a": 1}]}) == [{"a": 1}]
        assert extract_rows({"data": "not a list"}) == []
        assert extract_row_count({"total": 7}) == 7
        assert extract_row_count([{"a": 1}]) == -1

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([1, 2, None], "integer"),
            ([1, 2.5], "double"),
            ([True, False], "boolean"),
            (["a", 1], "varchar"),
            ([None, None], "varchar"),
            (["2024-01-01"], "varchar"),
        ],
    )
    def test_infer_column_type(self, values, expected):
        assert infer_column_type(values) == expected

    def test_build_filter_sql_escapes_quotes(self):
        sql = build_filter_sql("t", {"name": "O'Brien", "n": 3}, ["a", "b"], 10)
        assert sql == "SELECT a, b FROM t WHERE name = 'O''Brien' AND n = 3 LIMIT 10"

    def test_filter_rowset(self):
        rows = RowSet.from_records(USERS)
        out = filter_rowset(rows, {"active": True}, ["id", "name"], 1)
        assert out.to_records() == [{"id": 1, "name": "Alice"}]


def test_from_config_builds_real_transport_by_default():
    source = WarpSource.from_config(
        "db",
        {
            "base_url": "http://localhost:8080",
            "api_key": "k",
            "api_key_header": "X-Token",
            "timeout": 2,
            "max_retries": 0,
        },
    )
    assert isinstance(source._client, WarpHttpClient)
    assert source._client.database == "db"
    assert source._client._transport._pool.session.headers["X-Token"] == "k"  # type: ignore[attr-defined]
    source.close()


class TestFetchSlice:
    """The slice path: Arrow when offered, NDJSON next, paging as the floor."""

    def _rows(self, stream):
        try:
            return [dict(zip(b.columns, r, strict=True)) for b in stream for r in b.rows]
        finally:
            stream.close()

    def test_arrow_is_used_when_warp_offers_it(self):
        source, transport = _source()
        source.connect()
        spec = SliceSpec(columns=frozenset({"id", "name"}), predicates=(PRED_ACTIVE,))
        stream = source.fetch_slice("users", spec)
        assert stream.arrow_reader() is not None
        stream.close()
        method, url, params = transport.requests[-1]
        assert method == "GET" and url.endswith("/users/export")
        assert params["format"] == "arrow"
        assert params["fields"] == "id,name"
        assert params["filter[active][eq]"] == "true"

    def test_arrow_rows_keep_their_types(self):
        source, _ = _source()
        source.connect()
        rows = self._rows(source.fetch_slice("users", SliceSpec(predicates=(PRED_ACTIVE,))))
        assert [r["id"] for r in rows] == [1, 3]
        assert rows[0]["score"] == 95.5 and rows[1]["score"] == 70.0

    def test_ndjson_when_arrow_is_not_advertised(self):
        source, transport = _source(arrow=False)
        source.connect()
        rows = self._rows(source.fetch_slice("orders", SliceSpec.FULL))
        assert len(rows) == 5
        assert transport.requests[-1][2]["format"] == "ndjson"

    def test_unreadable_arrow_falls_back_to_ndjson_once(self):
        source, transport = _source(broken_arrow=b"not an arrow stream")
        source.connect()
        rows = self._rows(source.fetch_slice("orders", SliceSpec.FULL))
        assert len(rows) == 5
        formats = [p.get("format") for m, u, p in transport.requests if "export" in u]
        assert formats == ["arrow", "ndjson"]
        assert source.capabilities.arrow is False  # remembered for next time
        self._rows(source.fetch_slice("users", SliceSpec.FULL))
        assert [p.get("format") for m, u, p in transport.requests if "export" in u][-1] == "ndjson"

    def test_falls_back_to_paging_when_export_is_disabled(self):
        source, transport = _source(export_enabled=True)
        source.connect()
        transport.export_enabled = False  # Warp says yes, then answers 404
        rows = self._rows(source.fetch_slice("orders", SliceSpec.FULL))
        assert len(rows) == 5
        assert transport.urls("GET")[-1].endswith("/orders")
        # The next slice goes straight to paging; no second failed export.
        before = len([u for u in transport.urls("GET") if u.endswith("/export")])
        self._rows(source.fetch_slice("users", SliceSpec.FULL))
        assert len([u for u in transport.urls("GET") if u.endswith("/export")]) == before

    def test_legacy_warp_uses_the_list_endpoint_with_filters(self):
        source, transport = _source(mode="legacy")
        source.connect()
        spec = SliceSpec(columns=frozenset({"id"}), predicates=(PRED_ACTIVE,))
        rows = self._rows(source.fetch_slice("users", spec))
        assert rows == [{"id": 1}, {"id": 3}]
        params = transport.requests[-1][2]
        assert params["fields"] == "id" and params["filter[active][eq]"] == "true"

    def test_a_refused_slice_is_reported_not_masked(self):
        source, transport = _source()
        source.connect()
        spec = SliceSpec(predicates=(Predicate("nope", "eq", 1),))
        with pytest.raises(QueryError, match="refused"):
            source.fetch_slice("users", spec)

    def test_max_rows_and_the_warp_cap_are_both_applied(self):
        source, transport = _source(export_max_rows=2)
        source.connect()
        stream = source.fetch_slice("orders", SliceSpec.FULL)
        assert len(self._rows(stream)) == 2
        assert stream.row_limit == 2  # what the caller must treat as incomplete

    def test_row_limit_is_none_when_nothing_capped_the_read(self):
        source, _ = _source()
        source.connect()
        stream = source.fetch_slice("orders", SliceSpec.FULL)
        assert stream.row_limit is None
        stream.close()

    def test_long_in_lists_go_in_a_post_body(self):
        source, transport = _source()
        source.connect()
        keys = tuple(range(200))
        spec = SliceSpec(predicates=(Predicate("id", "in", keys),))
        self._rows(source.fetch_slice("orders", spec))
        method, url, payload = transport.requests[-1]
        assert method == "POST" and url.endswith("/orders/export")
        assert payload["filters"][0]["value"] == list(keys)
        assert payload["format"] == "arrow"


class TestEstimateSlice:
    def test_counts_matching_rows_through_the_list_endpoint(self):
        source, transport = _source()
        source.connect()
        assert source.estimate_slice("orders", SliceSpec.FULL) == 5
        assert source.estimate_slice("users", SliceSpec(predicates=(PRED_ACTIVE,))) == 2
        assert transport.requests[-1][2]["limit"] == 1  # one row, not the table

    def test_unknown_without_a_total_in_the_payload(self):
        source, _ = _source(mode="legacy", page_format="list")
        source.connect()
        assert source.estimate_slice("orders", SliceSpec.FULL) is None

    def test_unknown_when_the_request_fails(self):
        source, transport = _source()
        source.connect()
        transport.fail_next("HTTP 500", status=500)
        assert source.estimate_slice("orders", SliceSpec.FULL) is None

    def test_skipped_for_key_lists_too_long_for_a_url(self):
        source, transport = _source()
        source.connect()
        spec = SliceSpec(predicates=(Predicate("id", "in", tuple(range(500))),))
        before = len(transport.requests)
        assert source.estimate_slice("orders", spec) is None
        assert len(transport.requests) == before  # no request was even attempted
