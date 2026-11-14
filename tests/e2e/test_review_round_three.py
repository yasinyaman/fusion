"""Regressions for what the third review round found, mostly in earlier fixes.

Each test names the input that used to go wrong. They run over the fakes
(``FakeSourceFactory``, ``FakeWarpTransport``) and a real in-memory DuckDB.
"""

from dataclasses import replace

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from fusion.domain.errors import GuardrailViolation, QueryError
from fusion.domain.models import TableRef
from fusion.domain.slices import Predicate, SliceSpec, merge_same_key, pushable_form
from tests.conftest import _e2e_app
from tests.fakes import FakeWarpTransport

BIG = [
    {"id": i, "cid": i % 60, "status": "paid" if i % 2 else "new", "x": i % 3} for i in range(3000)
]
SMALL = [{"cid": i, "country": "TR" if i % 5 == 0 else "DE"} for i in range(20)]


@pytest.fixture
def big(app):
    app.sources.connect(
        "db",
        {"type": "fake", "tables": {"big": BIG, "small": SMALL}, "row_estimates": {"big": 600_000}},
    )
    return app


def _rows(app, sql):
    return app.query.sql(sql, use_cache=False).rows


class TestTheGuardrailReadsSqlAsDuckDbDoes:
    validator = SqlglotValidator()

    @pytest.mark.parametrize(
        "sql",
        [
            # DuckDB reads E'\'' as one escaped quote; a generic parse saw one
            # long string and missed the DROP after it.
            "SELECT E'\\'' ; DROP TABLE t; -- '",
            "SELECT E'\\'' ; UPDATE t SET a = 0; -- '",
            # Lenient parsing skipped what it could not read.
            "SELECT 1 DROP TABLE orders",
            "SELECT * FROM t FOR UPDATE",
            "EXPLAIN (ANALYZE) DELETE FROM t",
        ],
    )
    def test_refused(self, sql):
        with pytest.raises(GuardrailViolation):
            self.validator.validate(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "EXPLAIN (FORMAT JSON) SELECT 42",
            "SELECT 1;",
            "FROM t SELECT x",
            "EXPLAIN " * 500 + "SELECT 1",
        ],
    )
    def test_allowed(self, sql):
        self.validator.validate(sql)

    def test_end_to_end_the_table_survives(self, app_with_data):
        result = app_with_data.tools.execute(
            "query_data", {"sql": "SELECT E'\\'' ; DROP TABLE test_db.users; -- '"}
        )
        assert "error" in result
        assert app_with_data.store.count("test_db.users") == 5


class TestTheAnalyzerReadsSqlAsDuckDbDoes:
    analyzer = SqlglotAnalyzer()

    def test_a_column_alias_list_is_not_sliced(self):
        # ``b(cid, id)`` renames: cid here is the real id column.
        assert not self.analyzer.analyze(
            "SELECT * FROM db.big AS b(cid, id) WHERE cid = 100"
        ).is_simple_select

    def test_a_positional_join_is_not_sliced(self):
        shape = self.analyzer.analyze(
            "SELECT * FROM db.small POSITIONAL JOIN db.big b WHERE b.x = 1"
        )
        assert not shape.is_simple_select

    def test_an_escape_string_does_not_cut_the_where_short(self):
        # Read generically the WHERE ended at E, and x = 1 was pushed although
        # it sits under an OR. Read as DuckDB, the OR is seen: nothing pushed.
        sql = "SELECT count(*) FROM db.big WHERE x = 1 AND status = E'\\'' OR x = 2 --'"
        shape = self.analyzer.analyze(sql)
        assert all(not use.predicates for use in shape.tables)

    def test_pushdown_keeps_the_sql_as_written(self):
        sql = "SELECT id FROM ecommerce.orders ORDER BY amount DESC NULLS LAST LIMIT 3"
        assert self.analyzer.strip_source_prefix(sql, "ecommerce") == (
            "SELECT id FROM orders ORDER BY amount DESC NULLS LAST LIMIT 3"
        )


class TestALiteralGoesToTheSourceOnlyAsItWouldCompareThere:
    @pytest.mark.parametrize(
        ("predicate", "column_type", "sent"),
        [
            # DuckDB casts '1.005' to DECIMAL(10,2), i.e. 1.01; a source sent
            # 1.005 matches nothing.
            (Predicate("p", "eq", "1.005"), "decimal(10,2)", None),
            (Predicate("p", "eq", "1.5"), "double", 1.5),
            (Predicate("p", "eq", "12345678901234567.89"), "double", None),
            # Single precision: DuckDB casts the literal, the source widens the column.
            (Predicate("p", "eq", 0.1), "real", None),
            # An integer filter given 6.5 is a 400 at Warp.
            (Predicate("id", "gt", 6.5), "integer", None),
            (Predicate("id", "eq", 10.0), "integer", 10),
            (Predicate("a", "eq", "x"), "VARCHAR[]", None),
            (Predicate("u", "like", "%ab%"), "uuid", None),
            (Predicate("u", "eq", "not-a-uuid"), "uuid", None),
            # A zoned timestamp from text depends on who reads it, and a bare
            # date's "-15" is not an offset.
            (Predicate("ts", "gte", "2024-06-15"), "timestamp with time zone", None),
            (
                Predicate("ts", "gte", "2024-06-15 10:00Z"),
                "timestamp with time zone",
                "2024-06-15 10:00Z",
            ),
        ],
    )
    def test_rule(self, predicate, column_type, sent):
        form = pushable_form(predicate, column_type)
        assert (form.value if form is not None else None) == sent

    def test_warp_schema_types_are_kept(self):
        from fusion.adapters.outbound.warp.source import _column_type

        assert _column_type({"kind": "float", "type": "numeric", "precision": 10, "scale": 2}) == (
            "decimal(10,2)"
        )
        assert _column_type({"kind": "datetime", "type": "timestamp with time zone"}) == (
            "timestamp with time zone"
        )
        assert _column_type({"kind": "uuid"}) == "uuid"
        assert _column_type({"kind": "list"}) == "list"

    def test_the_store_reads_text_as_utc_by_default(self, app):
        assert app.query.sql("SELECT current_setting('TimeZone')", use_cache=False).rows == [
            ("UTC",)
        ]


class TestConditionsOnOneColumnAndOperator:
    def test_merged_exactly_where_they_can_be(self):
        merged, exact = merge_same_key(
            [
                Predicate("x", "gt", 5),
                Predicate("x", "gt", 10),
                Predicate("y", "in", (1, 2, 3)),
                Predicate("y", "in", (2, 3, 4)),
            ]
        )
        assert merged == (Predicate("x", "gt", 10), Predicate("y", "in", (2, 3)))
        assert exact

    def test_two_inequalities_keep_one_and_say_so(self):
        merged, exact = merge_same_key([Predicate("s", "ne", "a"), Predicate("s", "ne", "b")])
        assert merged == (Predicate("s", "ne", "a"),) and not exact

    def test_a_semi_join_key_list_intersects_the_querys_own(self):
        spec = SliceSpec(predicates=(Predicate("k", "in", (1, 3, 4, 5)),))
        assert spec.with_predicate(Predicate("k", "in", (3, 5, 9))).predicates == (
            Predicate("k", "in", (3, 5)),
        )

    def test_a_limit_survives_two_bounds_on_one_column(self, big, factory):
        assert _rows(big, "SELECT id FROM db.big WHERE id > 2990 AND id > 2995 LIMIT 3") == [
            (2996,),
            (2997,),
            (2998,),
        ]
        spec = factory.sources["db"].calls_named("fetch_slice")[-1][2]
        assert spec.predicates == (Predicate("id", "gt", 2995),) and spec.limit == 3


class TestEveryLoadAnswersToTheLimits:
    def test_a_search_slice_answers_to_the_slice_limit(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, full_load_max_rows=10, slice_max_rows=10, slice_budget_rows=20),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            rows = [{"id": i, "name": f"n{i}"} for i in range(100)]
            app.sources.connect("src", {"type": "fake", "tables": {"big": rows}})
            app.tools.execute(
                "search_data", {"table": "src.big", "filter_column": "name", "filter_value": "%"}
            )
            assert app.catalog.slice_rows_total() <= 20
        finally:
            app.close()

    def test_a_search_does_not_send_a_backslash_pattern(self, big, factory):
        big.sources.connect(
            "p",
            {"type": "fake", "tables": {"t": [{"path": "C:\\x"}]}, "row_estimates": {"t": 900_000}},
        )
        big.tools.execute(
            "search_data", {"table": "p.t", "filter_column": "path", "filter_value": "C:\\%"}
        )
        sent = [c[2] for c in factory.sources["p"].calls_named("fetch_slice")]
        assert all(not s.predicates for s in sent)

    def test_load_table_whole_evicts_to_fit_the_budget(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, full_load_max_rows=50, slice_budget_rows=60),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            tables = {"a": [{"v": i} for i in range(40)], "b": [{"v": i} for i in range(50)]}
            app.sources.connect("src", {"type": "fake", "tables": tables})
            app.tools.execute("load_table", {"table": "src.a"})
            app.tools.execute("load_table", {"table": "src.b"})
            assert app.catalog.slice_rows_total() <= 60
        finally:
            app.close()


class TestLoadsAndReads:
    def test_a_reuse_of_an_evicted_slice_loads_it_again(self, big):
        from fusion.domain.policy import TargetPlan

        ref = TableRef("db", "small")
        spec = SliceSpec.FULL
        big.sources.ensure_slices([TargetPlan(ref, spec, "load_full")])
        covering = big.catalog.find_covering_slice(ref, spec)
        big.sources.evict_slice(covering.table_name)
        mapping = big.sources.ensure_slices([TargetPlan(ref, spec, "reuse", covering=covering)])
        assert big.store.count(mapping[ref]) == 20

    def test_the_lock_table_does_not_keep_every_slice_name(self, app):
        import gc

        for i in range(50):
            with app.sources._load_lock(f"db.t__s_{i}"):
                pass
        gc.collect()
        assert len(app.sources._load_locks) == 0

    def test_a_lookup_without_a_clock_keeps_last_used(self, big):
        _rows(big, "SELECT count(*) FROM db.small")
        loaded = big.catalog.find_covering_slice(TableRef("db", "small"), SliceSpec.FULL, 123.0)
        big.catalog.find_covering_slice(TableRef("db", "small"), SliceSpec.FULL)
        assert loaded.last_used == 123.0

    def test_a_bare_table_name_is_found(self, big):
        assert _rows(big, "SELECT count(*) FROM small") == [(20,)]

    def test_a_source_table_named_mv_is_a_table(self, app):
        app.sources.connect("db", {"type": "fake", "tables": {"mv_daily": SMALL}})
        assert _rows(app, "SELECT count(*) FROM db.mv_daily") == [(20,)]

    def test_columns_go_to_the_source_spelled_as_the_catalog_has_them(self, big, factory):
        _rows(big, "SELECT ID FROM db.big WHERE X = 1 AND ID < 30")
        spec = factory.sources["db"].calls_named("fetch_slice")[-1][2]
        assert spec.columns == frozenset({"id", "x"})
        assert {p.column for p in spec.predicates} == {"id", "x"}

    def test_reserved_column_names_work_in_the_tools(self, app):
        rows = [{"order": i % 3, "group": "a" if i % 2 else "b"} for i in range(10)]
        app.sources.connect("db", {"type": "fake", "tables": {"t": rows}})
        agg = app.tools.execute(
            "aggregate_data",
            {"table": "db.t", "group_by": "group", "agg_column": "order", "agg_func": "sum"},
        )
        assert agg["row_count"] == 2
        assert (
            app.tools.execute(
                "search_data", {"table": "db.t", "filter_column": "group", "filter_value": "a"}
            )["row_count"]
            == 5
        )


class TestRefresh:
    def test_append_mode_keeps_a_row_that_arrived_in_the_newest_tick(
        self, settings, scheduler, factory
    ):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, refresh_config='{"db.e": {"watermark_column": "ts"}}'),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            rows = [{"id": 1, "ts": 100}, {"id": 2, "ts": 200}]
            app.sources.connect("db", {"type": "fake", "tables": {"e": rows}}, fetch_all=True)
            factory.sources["db"].set_table(
                "e", [*rows, {"id": 3, "ts": 200}, {"id": 4, "ts": 300}]
            )
            app.sources.refresh_all(force=True)
            assert _rows(app, "SELECT id FROM db.e ORDER BY id") == [(1,), (2,), (3,), (4,)]
        finally:
            app.close()

    def test_upsert_keys_are_case_insensitive_and_duplicates_are_replaced(self, app):
        from tests.fakes.data_source import ListRowStream

        store = app.store
        store.create_schema("s")
        store.materialize_stream(
            "s.t", ListRowStream.from_records([{"id": 1, "v": "a"}, {"id": 1, "v": "b"}])
        )
        store.upsert("s.t", ListRowStream.from_records([{"id": 1, "v": "a"}]), ["ID"])
        assert store.execute("SELECT v FROM s.t").rows == [("a",)]


class TestRestAndConfig:
    def test_a_body_over_the_limit_is_a_413(self, app_with_data):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        settings = replace(app_with_data.settings, max_request_bytes=1000)
        client = TestClient(create_app(app_with_data, settings))
        assert client.post("/query", json={"sql": "SELECT 1"}).status_code == 200
        assert client.post("/query", json={"sql": "SELECT 1 -- " + "x" * 5000}).status_code == 413

    def test_the_docs_are_off_in_production(self, app_with_data):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        settings = replace(
            app_with_data.settings,
            env="production",
            api_key="a-long-enough-production-key",
            cors_origins=("https://a",),
        )
        assert (
            TestClient(create_app(app_with_data, settings)).get("/openapi.json").status_code == 404
        )

    def test_readiness_does_not_wait_for_the_store(self, app_with_data):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        client = TestClient(create_app(app_with_data))
        with app_with_data.store._lock:  # a long query holds the store
            assert client.get("/readiness").status_code == 200

    def test_a_bad_aggregation_in_the_semantic_model_does_not_stop_startup(self):
        from fusion.application.settings import Settings

        raw = '{"db.t": {"measures": [{"name": "x", "agg": "nope"}]}}'
        assert Settings(semantic_model=raw).semantic_models() == {}

    def test_fusion_mcp_takes_its_log_level_from_the_environment(self):
        from fusion.adapters.inbound.cli.mcp_main import build_parser
        from fusion.application.settings import Settings

        assert build_parser(Settings(), "DEBUG").parse_args([]).log_level == "DEBUG"

    def test_an_unknown_time_zone_fails_validation(self):
        from fusion.application.settings import Settings

        assert Settings(timezone="Nope/Zone").validate()


class TestWarpReads:
    def test_a_refused_page_is_the_requests_fault_not_an_outage(self, settings, scheduler):
        transport = FakeWarpTransport(
            {"users": [{"id": i} for i in range(10)]},
            database="ecommerce",
            export_enabled=False,
            raw_query=False,
        )
        app = _e2e_app(settings, scheduler, transport)
        try:
            transport.fail_next("bad filter", status=400)
            result = app.tools.execute("query_data", {"sql": "SELECT * FROM ecommerce.users"})
            assert not result["error"].startswith("Source unavailable")
        finally:
            app.close()

    def test_a_body_cut_off_mid_read_is_an_outage(self):
        import requests

        from fusion.adapters.outbound.warp.streams import NdjsonRowStream
        from fusion.domain.errors import ConnectionError

        class Cut:
            def iter_lines(self):
                yield b'{"a": 1}'
                raise requests.exceptions.ChunkedEncodingError("connection broken")

            def close(self):
                pass

        with pytest.raises(ConnectionError, match="ended early"):
            list(NdjsonRowStream(Cut()))

    def test_a_source_without_limit_gets_the_query_uncapped(self, app, factory):
        app.sources.connect("m", {"type": "fake", "tables": {"t": SMALL}, "pushdown": True})
        source = factory.sources["m"]
        original = source.execute_query

        def no_limit(sql):
            if "LIMIT" in sql.upper():
                raise QueryError("Incorrect syntax near 'LIMIT'")
            return original(sql)

        source.execute_query = no_limit
        assert app.tools.execute("query_data", {"sql": "SELECT * FROM m.t"})["row_count"] == 20
        assert "m" in app.query._no_row_cap


def test_a_view_can_be_dropped_over_rest(app_with_data, scheduler):
    from fastapi.testclient import TestClient

    from fusion.adapters.inbound.rest.app import create_app

    client = TestClient(create_app(app_with_data))
    created = client.post(
        "/views", json={"name": "v", "sql": "SELECT 1 AS x", "refresh": "every 5 minutes"}
    )
    assert created.status_code == 200
    assert client.delete("/views/v").json() == {"status": "dropped", "name": "v"}
    assert client.delete("/views/v").status_code == 404
    assert scheduler.active_jobs == []
