"""Regressions for queries that used to return a wrong answer without an error.

Each case here came back with different rows than plain DuckDB gives for the
same data, because part of the query was applied at the source that only the
whole query can apply.
"""

from dataclasses import replace

import pytest

from fusion.adapters.outbound.sqlglot_policy import SqlglotAnalyzer
from fusion.adapters.outbound.warp.http import _fits_in_url, filter_params
from fusion.domain.errors import QueryError
from fusion.domain.slices import Predicate, SliceSpec, predicate_is_pushable
from tests.conftest import _e2e_app
from tests.fakes import FakeWarpTransport

BIG = [
    {"id": i, "cid": i % 60, "status": "paid" if i % 2 else "new", "x": i % 3, "zip": f"0{i}"}
    for i in range(3000)
]
SMALL = [{"cid": i, "country": "TR" if i % 5 == 0 else "DE"} for i in range(20)]


@pytest.fixture
def big(app):
    """``db.big`` is over the whole-table limit, so it can only be sliced."""
    app.sources.connect(
        "db",
        {"type": "fake", "tables": {"big": BIG, "small": SMALL}, "row_estimates": {"big": 600_000}},
    )
    return app


def _rows(app, sql):
    return app.query.sql(sql, use_cache=False).rows


class TestLimitIsNotReadAtTheSourceUnlessEverythingElseIs:
    def test_offset(self, big):
        sql = "SELECT id FROM db.big WHERE status = 'paid' LIMIT 5 OFFSET 20"
        assert _rows(big, sql) == [(41,), (43,), (45,), (47,), (49,)]

    def test_a_condition_the_source_cannot_apply(self, big):
        sql = "SELECT id FROM db.big WHERE status = 'paid' AND id % 7 = 0 AND id > 2000 LIMIT 5"
        assert _rows(big, sql) == [(2009,), (2023,), (2037,), (2051,), (2065,)]

    def test_a_plain_limit_still_is(self, big, factory):
        _rows(big, "SELECT id FROM db.big WHERE x = 1 LIMIT 3")
        assert factory.sources["db"].calls_named("fetch_slice")[-1][2].limit == 3

    def test_not_beside_a_condition_on_text(self, big, factory):
        # A case-insensitive source answers status = 'paid' with 'PAID' too;
        # three such rows, dropped here, would leave fewer than three.
        _rows(big, "SELECT id FROM db.big WHERE status = 'paid' LIMIT 3")
        assert factory.sources["db"].calls_named("fetch_slice")[-1][2].limit is None

    @pytest.mark.parametrize("limit", ["LIMIT 10 PERCENT", "LIMIT 10%"])
    def test_a_percentage(self, big, limit):
        # 1,500 paid rows: ten percent is 150, not ten percent of ten.
        assert len(_rows(big, f"SELECT id FROM db.big WHERE status = 'paid' {limit}")) == 150

    def test_two_conditions_with_one_column_and_operator(self, big, factory):
        sql = "SELECT x FROM db.big WHERE x != 0 AND x != 1 LIMIT 1"
        assert _rows(big, sql) == [(2,)]
        spec = big.query.planner.plan_query(sql).targets[0].spec
        assert len(spec.predicates) == 1 and spec.limit is None


class TestJoinsThatAreNotInner:
    analyzer = SqlglotAnalyzer()

    @pytest.mark.parametrize(
        "join",
        [
            "ANTI JOIN db.small s ON b.cid = s.cid",
            "SEMI JOIN db.small s ON b.cid = s.cid",
            "ASOF JOIN db.small s ON b.cid >= s.cid",
            "JOIN db.small s USING (cid)",
            "NATURAL JOIN db.small s",
        ],
    )
    def test_are_not_sliced(self, join):
        assert not self.analyzer.analyze(f"SELECT b.id FROM db.big b {join}").is_simple_select

    def test_anti_join_refuses_instead_of_fetching_only_the_matches(self, big):
        with pytest.raises(QueryError, match="Refusing to load db.big"):
            _rows(big, "SELECT count(*) FROM db.big b ANTI JOIN db.small s ON b.cid = s.cid")

    @pytest.mark.parametrize("join", ["LEFT JOIN", "LEFT OUTER JOIN"])
    def test_an_outer_join_is_sliced_however_it_is_spelled(self, big, join):
        sql = f"SELECT b.id, s.country FROM db.big b {join} db.small s ON b.cid = s.cid "
        sql += "WHERE b.id = 7"
        assert _rows(big, sql) == [(7, "DE")]

    def test_a_query_on_a_loaded_table_does_not_wait_for_its_reload(self, app):
        import threading

        app.sources.connect("db", {"type": "fake", "tables": {"small": SMALL}})
        assert _rows(app, "SELECT count(*) FROM db.small") == [(20,)]
        # Stand in for a reload in progress: another thread holds the table's load lock.
        held, release = threading.Event(), threading.Event()

        def reload_in_progress():
            with app.sources._load_lock(app.catalog.list_tables()[0].full_name):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=reload_in_progress)
        worker.start()
        held.wait(5)
        try:
            done: list = []
            reader = threading.Thread(
                target=lambda: done.append(_rows(app, "SELECT count(*) FROM db.small"))
            )
            reader.start()
            reader.join(2)
            assert done == [[(20,)]]
        finally:
            release.set()
            worker.join()

    def test_inner_join_is_still_fetched_by_key(self, big):
        sql = (
            "SELECT count(*) FROM db.big b JOIN db.small s ON b.cid = s.cid WHERE s.country = 'TR'"
        )
        assert _rows(big, sql) == [(200,)]

    def test_keys_are_not_passed_to_a_column_of_another_kind(self, big):
        # DuckDB matches cid 7 to zip '07'; a source asked for zip IN (7, ...)
        # would not, so refusing is the only answer that is not wrong.
        sql = "SELECT count(*) FROM db.small s JOIN db.big b ON s.cid = b.zip"
        assert big.query.planner.plan_query(sql).is_refused


class TestALiteralIsPushedOnlyAgainstItsOwnKindOfColumn:
    @pytest.mark.parametrize(
        ("predicate", "column_type", "expected"),
        [
            (Predicate("zip", "eq", 123), "varchar", False),
            # Recast to the column's kind, as DuckDB casts the literal.
            (Predicate("amount", "gt", "10"), "double", True),
            (Predicate("id", "eq", "4.5"), "integer", False),
            (Predicate("amount", "gt", 10), "double", True),
            (Predicate("id", "in", (1, 2)), "bigint", True),
            (Predicate("name", "eq", "a"), "varchar", True),
            (Predicate("d", "gte", "2024-01-01"), "date", True),
            (Predicate("flag", "eq", True), "boolean", True),
            (Predicate("flag", "eq", 1), "boolean", True),
            (Predicate("flag", "eq", 2), "boolean", False),
            # The source's collation decides these on text.
            (Predicate("name", "gt", "a"), "varchar", False),
            (Predicate("name", "ne", "a"), "varchar", False),
            (Predicate("name", "like", "a\\_b"), "varchar", False),
            (Predicate("name", "like", "a%"), "varchar", True),
            (Predicate("n", "eq", True), "integer", False),
            (Predicate("anything", "is_null", True), None, True),
            (Predicate("payload", "eq", "x"), "json", False),
            (Predicate("mystery", "eq", "x"), None, False),
        ],
    )
    def test_rule(self, predicate, column_type, expected):
        assert predicate_is_pushable(predicate, column_type) is expected

    def test_number_against_a_text_column_stays_local(self, big):
        plan = big.query.planner.plan_query("SELECT id FROM db.big WHERE zip = 123 AND id < 9")
        assert plan.targets[0].spec.predicates == (Predicate("id", "lt", 9),)

    def test_load_table_says_so(self, big):
        result = big.tools.execute("load_table", {"table": "db.big", "where": "zip = 123"})
        assert "on column 'zip'" in result["error"]

    def test_a_quoted_number_on_a_number_column_is_sent_as_a_number(self, big):
        plan = big.query.planner.plan_query("SELECT id FROM db.big WHERE id = '42'")
        assert plan.targets[0].spec.predicates == (Predicate("id", "eq", 42),)
        assert _rows(big, "SELECT id FROM db.big WHERE id = '42'") == [(42,)]


class TestASliceCoversOnlyWhatItProvablyHolds:
    @pytest.mark.parametrize(
        ("held", "asked"),
        [
            (Predicate("amount", "gt", "10"), Predicate("amount", "gt", "9")),
            (Predicate("d", "gte", "2024-10-1"), Predicate("d", "gte", "2024-2-1")),
            # 'T' sorts after ' ' whatever the time on either side of it.
            (Predicate("t", "gte", "2024-01-01 12:00"), Predicate("t", "gte", "2024-01-01T10:00")),
            (Predicate("t", "lt", "2024-01-01T10:00"), Predicate("t", "lt", "2024-01-01 12:00")),
        ],
    )
    def test_text_that_orders_differently_as_a_number_or_date(self, held, asked):
        assert not SliceSpec(predicates=(held,)).subsumes(SliceSpec(predicates=(asked,)))

    def test_numbers_and_iso_dates_still_narrow(self):
        assert Predicate("a", "gt", 10).implies(Predicate("a", "gt", 9))
        assert Predicate("d", "gte", "2024-10-01").implies(Predicate("d", "gte", "2024-02-01"))


class TestInListOverHttp:
    spec = SliceSpec(predicates=(Predicate("name", "in", ("x, y", "apple")),))

    def test_a_value_with_a_comma_goes_in_the_body(self):
        assert not _fits_in_url("http://w/api/v1/db/t/export", self.spec, None, "json", None)

    def test_and_is_refused_where_only_the_url_is_available(self):
        with pytest.raises(QueryError, match="comma"):
            filter_params(self.spec.predicates)

    def test_two_filters_on_one_column_and_operator_do_not_overwrite_each_other(self):
        spec = SliceSpec(predicates=(Predicate("s", "ne", "a"), Predicate("s", "ne", "b")))
        assert not _fits_in_url("http://w/api/v1/db/t/export", spec, None, "json", None)
        with pytest.raises(QueryError, match="same column and operator"):
            filter_params(spec.predicates)


class TestToolRows:
    def test_two_columns_with_one_name_are_both_returned(self, app):
        result = app.tools.execute("query_data", {"sql": "SELECT 1 AS id, 2 AS id"})
        assert result["columns"] == ["id", "id_2"]
        assert result["rows"] == [{"id": 1, "id_2": 2}]

    def test_a_suffix_never_takes_the_name_of_a_real_column(self, app):
        result = app.tools.execute("query_data", {"sql": "SELECT 1 AS id, 2 AS id_2, 3 AS id"})
        assert result["columns"] == ["id", "id_2", "id_3"]
        assert result["rows"] == [{"id": 1, "id_2": 2, "id_3": 3}]


class TestSemanticWindows:
    ROWS = [
        {"m": m, "region": region, "amt": value, "w": 1, "p": p}
        for region, values in (("east", (10, 20, 30)), ("west", (100, 200, 300)))
        for m, value, p in zip((1, 2, 3), values, (10, None, 20), strict=True)
    ]

    @pytest.fixture
    def run(self, app):
        app.sources.connect("db", {"type": "fake", "tables": {"t": self.ROWS}}, fetch_all=True)

        def call(**arguments):
            return app.tools.execute("query_metrics", {"table": "db.t", **arguments})

        return call

    def test_each_other_dimension_is_its_own_series(self, run):
        result = run(metrics=["cumsum(amt:sum)"], dimensions=["m", "region"])
        totals = {(r["m"], r["region"]): r["cumsum_amt_sum"] for r in result["rows"]}
        assert [totals[m, "east"] for m in (1, 2, 3)] == [10, 30, 60]
        assert [totals[m, "west"] for m in (1, 2, 3)] == [100, 300, 600]

    def test_two_metrics_with_one_name_are_refused(self, run):
        result = run(metrics=["lag(amt:sum, n=1)", "lag(amt:sum, n=2)"], dimensions=["m"])
        assert "both be called lag_amt_sum" in result["error"]

    def test_is_null_without_a_value_means_is_null(self, run):
        result = run(metrics=["*:count"], filters=[{"column": "p", "op": "is_null"}])
        assert result["rows"] == [{"count": 2}]

    def test_is_null_with_a_null_value_means_is_null(self, run):
        filters = [{"column": "p", "op": "is_null", "value": None}]
        assert run(metrics=["*:count"], filters=filters)["rows"] == [{"count": 2}]

    def test_is_null_with_a_value_that_is_not_a_boolean_is_refused(self, run):
        filters = [{"column": "p", "op": "is_null", "value": "false"}]
        assert "takes true, false or no value" in run(metrics=["*:count"], filters=filters)["error"]

    def test_weighted_avg_ignores_the_weight_of_a_missing_value(self, run):
        result = run(metrics=["p:weighted_avg(weight=w)"])
        assert list(result["rows"][0].values()) == [15.0]


class TestFreshness:
    def test_refreshing_a_view_re_reads_its_tables(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": {"small": SMALL}})
        app.tools.execute("create_view", {"name": "v", "sql": "SELECT count(*) AS n FROM db.small"})
        factory.sources["db"].set_table("small", [*SMALL, {"cid": 999, "country": "US"}])
        app.tools.execute("refresh_view", {"name": "v"})
        assert _rows(app, "SELECT n FROM mv_v") == [(21,)]

    def test_a_row_changed_inside_the_newest_tick_is_picked_up(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        rows = [{"id": 1, "v": "a", "at": 100}, {"id": 2, "v": "b", "at": 100}]
        app = build_app(
            replace(
                settings,
                refresh_config='{"db.t": {"watermark_column": "at", "key_columns": ["id"]}}',
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": {"t": rows}}, fetch_all=True)
            factory.sources["db"].set_table("t", [rows[0], {"id": 2, "v": "B", "at": 100}])
            app.sources.refresh_all(force=True)
            assert _rows(app, "SELECT id, v FROM db.t ORDER BY id") == [(1, "a"), (2, "B")]
        finally:
            app.close()

    def test_a_row_with_a_null_key_is_not_added_again_on_each_refresh(
        self, settings, scheduler, factory
    ):
        from fusion.bootstrap import build_app

        rows = [{"id": 1, "v": "a", "at": 100}, {"id": None, "v": "b", "at": 100}]
        app = build_app(
            replace(
                settings,
                refresh_config='{"db.t": {"watermark_column": "at", "key_columns": ["id"]}}',
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": {"t": rows}}, fetch_all=True)
            for _ in range(3):
                app.sources.refresh_all(force=True)
            assert _rows(app, "SELECT count(*) FROM db.t") == [(2,)]
        finally:
            app.close()


class TestSemiJoinOverACappedSource:
    def test_refuses_rather_than_join_over_the_rows_it_was_given(self, settings, scheduler):
        users = [{"id": i, "name": f"u{i}"} for i in range(5)]
        orders = [{"id": i, "user_id": i % 5} for i in range(1000)]
        transport = FakeWarpTransport(
            {"users": users, "orders": orders}, database="ecommerce", export_max_rows=100
        )
        app = _e2e_app(replace(settings, full_load_max_rows=500), scheduler, transport)
        try:
            with pytest.raises(QueryError, match="stopped at its own limit"):
                app.query.sql(
                    "SELECT count(*) FROM ecommerce.users u JOIN ecommerce.orders o "
                    "ON u.id = o.user_id WHERE u.id < 3"
                )
        finally:
            app.close()


class TestLoadLimitsCannotBeSidestepped:
    @pytest.fixture
    def huge(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, full_load_max_rows=3, slice_max_rows=10),
            scheduler=scheduler,
            source_factory=factory,
        )
        rows = [{"id": i, "name": f"n{i}"} for i in range(50)]
        app.sources.connect(
            "db", {"type": "fake", "tables": {"t": rows}, "row_estimates": {"t": 9_000_000}}
        )
        yield app
        app.close()

    @pytest.mark.parametrize(
        "arguments",
        [{}, {"where": ""}, {"columns": ["id", "name"]}, {"where": "id >= 0"}],
    )
    def test_load_table(self, huge, arguments):
        result = huge.tools.execute("load_table", {"table": "db.t", **arguments})
        assert "Refusing to load" in result["error"]
        assert not huge.catalog.slices_of("db.t")

    def test_create_view(self, huge):
        result = huge.tools.execute("create_view", {"name": "v", "sql": "SELECT * FROM db.t"})
        assert "loads the tables it reads whole, and db.t" in result["error"]
        # Only remedies that work for a view: no "add a WHERE", no load_table.
        assert "query_data" in result["error"] and "load_table" not in result["error"]
        assert not huge.catalog.is_loaded("db.t")

    def test_a_failed_view_refresh_leaves_no_stale_cache(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": {"small": SMALL, "t2": SMALL}})
        sql = "SELECT count(*) AS n FROM db.small s JOIN db.t2 t ON s.cid = t.cid"
        app.tools.execute("create_view", {"name": "v", "sql": sql})
        assert app.query.sql("SELECT count(*) FROM db.small").rows == [(20,)]
        factory.sources["db"].set_table("small", [*SMALL, {"cid": 99, "country": "US"}])
        source = factory.sources["db"]
        original = source.fetch_slice

        def fail_on_t2(table, spec, max_rows=None):
            if table == "t2":
                raise QueryError("t2 is unavailable")
            return original(table, spec, max_rows=max_rows)

        source.fetch_slice = fail_on_t2
        assert "error" in app.tools.execute("refresh_view", {"name": "v"})
        # small was re-read before t2 failed, so the cached 20 is out of date.
        assert app.query.sql("SELECT count(*) FROM db.small").rows == [(21,)]


class TestViewTimers:
    def test_re_creating_a_view_cancels_its_old_timer(self, app, scheduler):
        for _ in range(2):
            app.views.create("v", "SELECT 1 AS n", refresh="every 5 minutes")
        assert [job.name for job in scheduler.active_jobs] == ["mv:v"]

    @pytest.mark.parametrize("refresh", ["every 1 seconds", "every 29 seconds"])
    def test_an_interval_shorter_than_the_minimum_is_refused(self, app, refresh):
        with pytest.raises(QueryError, match="too short"):
            app.views.create("v", "SELECT 1 AS n", refresh=refresh)

    def test_trailing_text_is_not_an_interval(self):
        from fusion.domain.views import parse_refresh_interval

        assert parse_refresh_interval("every 5 minutes; drop everything") == 0


class TestEnvironmentName:
    @pytest.mark.parametrize("value", ["Production", " production ", "PROD"])
    def test_spelling_variants_are_production(self, value):
        from fusion.application.settings import Settings

        settings = Settings.from_env({"FUSION_ENV": value})
        assert settings.is_production()
        assert any("FUSION_API_KEY" in error for error in settings.validate())

    def test_an_unknown_name_fails_validation(self):
        from fusion.application.settings import Settings

        errors = Settings.from_env({"FUSION_ENV": "prodution"}).validate()
        assert any("FUSION_ENV" in error for error in errors)


def test_timestamptz_values_can_be_returned(app):
    result = app.tools.execute("query_data", {"sql": "SELECT now() AS n"})
    assert "error" not in result


class TestFileAccessGuardrail:
    """Each of these read a local file when DuckDB's external access was on."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM '/etc/passwd'",
            'SELECT * FROM "/tmp/data.csv"',
            "SELECT * FROM 's3://bucket/key.parquet'",
            "SELECT * FROM \"read_csv\"('/etc/passwd')",
            "SELECT 1 -- it's\nUNION ALL SELECT count(*) FROM read_csv('/etc/passwd')",
            "SELECT * FROM READ_JSON_AUTO('/tmp/x.json')",
            "SELECT * FROM t JOIN 'other.parquet' USING (id)",
        ],
    )
    def test_blocked(self, sql):
        from fusion.adapters.outbound.sqlglot_policy import SqlglotValidator
        from fusion.domain.errors import GuardrailViolation

        with pytest.raises(GuardrailViolation):
            SqlglotValidator().validate(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT o.id FROM db.orders o JOIN db.orders__s_0e4acba6cf s ON o.id = s.id",
            "SELECT * FROM logs WHERE path = '/etc/passwd' AND note LIKE '%.csv'",
            "SELECT read_count, load FROM metrics",
        ],
    )
    def test_ordinary_queries_still_pass(self, sql):
        from fusion.adapters.outbound.sqlglot_policy import SqlglotValidator

        SqlglotValidator().validate(sql)

    def test_sql_that_cannot_be_parsed_is_refused(self):
        from fusion.adapters.outbound.sqlglot_policy import SqlglotValidator
        from fusion.domain.errors import GuardrailViolation

        with pytest.raises(GuardrailViolation, match="could not be parsed"):
            SqlglotValidator().validate("SET threads=1 /* never closed")

    def test_end_to_end_with_external_access_on(self, tmp_path, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        secret = tmp_path / "secret.csv"
        secret.write_text("a,b\n1,topsecret\n")
        app = build_app(
            replace(settings, external_access=True), scheduler=scheduler, source_factory=factory
        )
        try:
            result = app.tools.execute("query_data", {"sql": f"SELECT * FROM '{secret}'"})
            assert "topsecret" not in str(result)
            assert "Blocked" in result["error"]
        finally:
            app.close()


class TestResultCap:
    def test_rows_past_the_cap_are_not_read_or_cached(self, app):
        result = app.tools.execute("query_data", {"sql": "SELECT * FROM range(3000000)"})
        assert (result["row_count"], result["truncated"]) == (100, True)
        cached = app.cache._entries  # noqa: SLF001 - the point is what the cache holds
        assert [len(entry[0].rows) for entry in cached.values()] == [101]

    def test_the_capped_answer_is_not_served_to_a_caller_who_wants_all_of_it(self, app):
        app.tools.execute("query_data", {"sql": "SELECT * FROM range(250)"})
        assert app.query.sql("SELECT * FROM range(250)").row_count == 250


class TestQueryTimeout:
    SLOW = "SELECT count(*) FROM range(200000) a, range(200000) b WHERE a.range + b.range = 7"

    def test_a_query_past_the_timeout_is_stopped_and_the_store_stays_usable(
        self, settings, scheduler, factory
    ):
        import time

        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, query_timeout=0.3), scheduler=scheduler, source_factory=factory
        )
        try:
            started = time.perf_counter()
            with pytest.raises(QueryError, match="FUSION_QUERY_TIMEOUT"):
                app.query.sql(self.SLOW, use_cache=False)
            assert time.perf_counter() - started < 5
            assert app.query.sql("SELECT 42", use_cache=False).rows == [(42,)]
        finally:
            app.close()

    def test_building_a_view_answers_to_the_timeout_too(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, query_timeout=0.3), scheduler=scheduler, source_factory=factory
        )
        try:
            result = app.tools.execute("create_view", {"name": "slow", "sql": self.SLOW})
            assert "FUSION_QUERY_TIMEOUT" in result["error"]
        finally:
            app.close()


class TestWarpRetries:
    def test_a_raw_query_is_sent_once_and_an_export_is_retried(self):
        from fusion.adapters.outbound.warp.connection_pool import ConnectionPool

        pool = ConnectionPool(max_retries=3)
        try:
            retried = pool.session.get_adapter("http://x").max_retries
            assert "POST" in retried.allowed_methods  # an export POST is a read
            assert 500 not in retried.status_forcelist  # asking again gets the same 500
            assert pool._once.get_adapter("http://x").max_retries.total == 0
        finally:
            pool.close()

    def test_a_500_does_not_count_against_the_source_on_any_route(self):
        from fusion.adapters.outbound.warp.http import HttpTransportError, is_transport_failure

        base = "http://w/api/v1/db"
        for route in ("/query/execute", "/t", "/t/export"):
            # Warp answers 500 when the database rejects the statement.
            assert not is_transport_failure(
                HttpTransportError("HTTP 500", status=500, url=base + route)
            )
        for status in (502, 503, 504, 408, 429):
            assert is_transport_failure(HttpTransportError("x", status=status, url=base + "/t"))


class TestRateLimitOrder:
    def test_guessing_keys_is_rate_limited(self, app_with_data):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        settings = replace(app_with_data.settings, api_key="the-real-key", rate_limit="3/minute")
        client = TestClient(create_app(app_with_data, settings))
        codes = [
            client.get("/sources", headers={"X-API-Key": f"guess-{i}"}).status_code
            for i in range(6)
        ]
        assert codes == [403, 403, 403, 429, 429, 429]

    @pytest.mark.parametrize(("method", "path"), [("get", "/nope"), ("get", "/query")])
    def test_also_where_the_limiter_has_no_route_limit(self, app_with_data, method, path):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        settings = replace(app_with_data.settings, api_key="the-real-key", rate_limit="3/minute")
        client = TestClient(create_app(app_with_data, settings))
        codes = [
            getattr(client, method)(path, headers={"X-API-Key": f"guess-{i}"}).status_code
            for i in range(5)
        ]
        assert codes == [403, 403, 403, 429, 429]


class TestSettingsRanges:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"rate_limit": "garbage"},
            {"rate_limit": "0/minute"},
            {"cache_max_entries": 0},
            {"backup_enabled": True, "backup_interval": 0},
            {"backup_enabled": True, "backup_retention_days": 0},
            {"threads": 0},
            {"port": 99999},
            {"warp_timeout": float("nan")},
            {"in_chunk_size": 0},
            {"env": "production", "api_key": "short"},
            # Long enough, so only the case-insensitive placeholder check refuses it.
            {
                "env": "production",
                "api_key": "Your-Secure-API-Key-Here-Change-In-Production",
                "cors_origins": ("https://a",),
            },
            {"rate_limit": "1٠/minute"},  # a digit limits does not read
            {"circuit_breaker_timeout": 0},
            {"query_timeout": float("inf")},
        ],
    )
    def test_rejected(self, overrides):
        from fusion.application.settings import Settings

        assert Settings(**overrides).validate()

    def test_the_backup_schedule_is_checked_only_when_backups_run(self):
        from fusion.application.settings import Settings

        assert Settings(backup_interval=0, backup_retention_days=0).validate() == []

    def test_the_defaults_and_common_rate_forms_pass(self):
        from fusion.application.settings import Settings

        assert Settings().validate() == []
        assert Settings(rate_limit="5 per minute; 100/hour").validate() == []

    @pytest.mark.parametrize(
        "rate", ["100/minute,1000/hour", "100/minute|5/second", "1000/month", "10 per 5 minutes"]
    )
    def test_every_form_the_limiter_reads_passes(self, rate):
        from fusion.application.settings import Settings

        assert Settings(rate_limit=rate).validate() == []

    def test_the_mcp_server_skips_what_only_http_uses(self):
        from fusion.application.settings import Settings

        settings = Settings(env="production", api_key="", rate_limit="garbage")
        assert settings.validate(http=False) == []
        assert Settings(env="prodution").validate(http=False)

    def test_fusion_mcp_refuses_to_start_on_a_bad_setting(self):
        from fusion.adapters.inbound.cli.mcp_main import main

        assert main([], {"FUSION_ENV": "prodution"}) == 1

    @pytest.mark.parametrize("value", ["1", "yes", "ON", "true"])
    def test_truthy_spellings_enable_a_flag(self, value):
        from fusion.application.settings import Settings

        assert Settings.from_env({"FUSION_BACKUP_ENABLED": value}).backup_enabled is True
        assert Settings.from_env({"FUSION_DEBUG_ENDPOINTS": value}).debug_endpoints_enabled()


class TestRestErrors:
    @pytest.fixture
    def client(self, app_lazy):
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app

        return TestClient(create_app(app_lazy), raise_server_exceptions=False)

    def test_an_unhandled_error_keeps_its_request_id(self, client, app_lazy, monkeypatch):
        def boom(sql):
            raise RuntimeError("secret detail")

        monkeypatch.setattr(app_lazy.tools, "query_data", boom)
        response = client.post("/query", json={"sql": "SELECT 1"}, headers={"X-Request-ID": "r-1"})
        assert response.status_code == 500
        assert response.headers["X-Request-ID"] == "r-1"
        assert response.json() == {"error": "Internal server error", "request_id": "r-1"}

    @pytest.fixture
    def down(self, app_lazy, factory):
        """A source with no pushdown, so every query has to fetch from it."""
        app_lazy.sources.connect("db", {"type": "fake", "tables": {"small": SMALL}})
        factory.sources["db"].connected = False
        return app_lazy

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/query", {"sql": "SELECT * FROM db.small"}),
            ("/tools/query_data", {"sql": "SELECT * FROM db.small"}),
            ("/tools/load_table", {"table": "db.small"}),
        ],
    )
    def test_a_source_that_is_down_is_a_503(self, client, down, path, body):
        assert client.post(path, json=body).status_code == 503

    def test_a_tool_says_the_source_is_unavailable(self, down):
        result = down.tools.execute("query_data", {"sql": "SELECT * FROM db.small"})
        assert result["error"].startswith("Source unavailable: Not connected")

    def test_a_warp_that_is_down_is_unavailable_on_the_export_path(self, e2e_app, e2e_transport):
        for _ in range(2):  # the pushdown attempt, then the export it falls back to
            e2e_transport.fail_next("connection refused", status=None)
        result = e2e_app.tools.execute("query_data", {"sql": "SELECT * FROM ecommerce.users"})
        assert result["error"].startswith("Source unavailable")

    def test_a_name_that_says_blocked_is_not_a_guardrail_refusal(self, client):
        # Through the tool dispatch, where the status is read off the message.
        assert client.post("/tools/refresh_view", json={"name": "Blocked"}).status_code == 400

    def test_an_internal_error_is_a_500_without_the_exception_text(self, client):
        response = client.post("/tools/query_data", json={"sql": None})
        assert response.status_code == 500
        assert "NoneType" not in response.text


def test_two_backups_in_one_second_are_two_backups(tmp_path, settings, scheduler, factory):
    from fusion.bootstrap import build_app

    app = build_app(
        replace(
            settings,
            database=str(tmp_path / "f.duckdb"),
            backup_enabled=True,
            backup_path=str(tmp_path / "backups"),
        ),
        scheduler=scheduler,
        source_factory=factory,
    )
    try:
        assert app.backup.create_backup().name != app.backup.create_backup().name
        app.query.sql("SELECT 1")
        app.backup.restore_backup(app.backup.list_backups()[0].path)
        assert app.cache.stats()["cached_queries"] == 0
    finally:
        app.close()


class TestSecondRound:
    """The findings left after the first round of fixes."""

    def test_different_slices_of_one_table_load_side_by_side(self, app, factory):
        import threading
        import time

        app.sources.connect("db", {"type": "fake", "tables": {"big": BIG}})
        source = factory.sources["db"]
        original = source.fetch_slice
        inside, peak, guard = [0], [0], threading.Lock()

        def slow(table, spec, max_rows=None):
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.3)
            with guard:
                inside[0] -= 1
            return original(table, spec, max_rows=max_rows)

        source.fetch_slice = slow
        specs = [
            SliceSpec(predicates=(Predicate("x", "eq", 0),)),
            SliceSpec(predicates=(Predicate("x", "eq", 1),)),
        ]
        from fusion.domain.models import TableRef
        from fusion.domain.policy import TargetPlan

        ref = TableRef("db", "big")
        threads = [
            threading.Thread(
                target=app.sources.ensure_slices, args=([TargetPlan(ref, s, "load_slice")],)
            )
            for s in specs
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak[0] == 2

    def test_a_request_that_waited_reuses_the_slice_it_waited_for(self, app, factory):
        import threading
        import time

        from fusion.domain.models import TableRef
        from fusion.domain.policy import TargetPlan

        app.sources.connect("db", {"type": "fake", "tables": {"big": BIG}})
        ref = TableRef("db", "big")
        spec = SliceSpec(predicates=(Predicate("x", "eq", 0),))
        target = TargetPlan(ref, spec, "load_slice")
        lock = app.sources._load_lock(spec.table_name_for(ref))
        lock.acquire()
        waiter = threading.Thread(target=app.sources.ensure_slices, args=([target],))
        waiter.start()
        time.sleep(0.2)  # let the waiter reach the lock and block on it
        try:
            app.sources.ensure_slices([target])  # the "first" request, same thread: reentrant
        finally:
            lock.release()
        waiter.join()
        assert len(factory.sources["db"].calls_named("fetch_slice")) == 1

    def test_a_pushed_down_query_asks_the_source_for_101_rows(self, app_lazy, factory):
        app_lazy.tools.execute("query_data", {"sql": "SELECT * FROM warp_main.users"})
        sent = factory.sources["warp_main"].calls_named("execute_query")[-1][1]
        assert sent.rstrip().endswith("LIMIT 101")

    def test_one_watchdog_thread_serves_every_query(self, settings, scheduler, factory):
        import threading

        from fusion.bootstrap import build_app

        app = build_app(
            replace(settings, query_timeout=30), scheduler=scheduler, source_factory=factory
        )
        try:
            before = threading.active_count()
            for _ in range(50):
                app.query.sql("SELECT 1", use_cache=False)
            assert threading.active_count() - before <= 1
        finally:
            app.close()

    def test_views_on_one_table_read_it_once_per_pass(self, app, factory):
        app.sources.connect("db", {"type": "fake", "tables": {"small": SMALL}})
        for name in ("a", "b", "c"):
            app.tools.execute("create_view", {"name": name, "sql": "SELECT count(*) FROM db.small"})
        before = len(factory.sources["db"].calls_named("fetch_slice"))
        app.views.refresh_all()
        # The tables were just loaded by create_view, so this pass reads none.
        assert len(factory.sources["db"].calls_named("fetch_slice")) == before

    def test_an_unchanged_newest_tick_rewrites_nothing(self, settings, scheduler, factory):
        from fusion.bootstrap import build_app

        rows = [{"id": i, "v": "a", "at": 100} for i in range(5)]
        app = build_app(
            replace(
                settings,
                refresh_config='{"db.t": {"watermark_column": "at", "key_columns": ["id"]}}',
            ),
            scheduler=scheduler,
            source_factory=factory,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": {"t": rows}}, fetch_all=True)
            spec = app.sources._refresh_specs["db.t"]
            ref = app.catalog.list_tables()[0]
            assert app.sources._refresh_incrementally(ref, factory.sources["db"], spec) == 5
            assert (
                app.store.upsert(
                    "db.t",
                    factory.sources["db"].fetch_slice("t", SliceSpec.FULL),
                    ["id"],
                )
                == 0
            )
        finally:
            app.close()

    def test_an_eviction_waits_for_a_load_of_the_same_slice(self, app):
        import threading

        app.sources.connect("db", {"type": "fake", "tables": {"small": SMALL}})
        lock = app.sources._load_lock("db.small__s_x")
        lock.acquire()
        done = threading.Event()
        evictor = threading.Thread(
            target=lambda: (app.sources.evict_slice("db.small__s_x"), done.set())
        )
        evictor.start()
        assert not done.wait(0.2)
        lock.release()
        evictor.join(2)
        assert done.is_set()


def test_paged_reads_are_ordered_by_the_primary_key(settings, scheduler):
    # OFFSET over an unordered scan may skip or repeat rows between pages.
    transport = FakeWarpTransport(
        {"users": [{"id": i, "name": f"u{i}"} for i in range(30)]},
        database="ecommerce",
        export_enabled=False,
        raw_query=False,
    )
    app = _e2e_app(settings, scheduler, transport)
    try:
        assert app.query.sql("SELECT count(*) FROM ecommerce.users").rows == [(30,)]
        pages = [p for m, u, p in transport.requests if "offset" in (p or {})]
        assert pages and all(p.get("sort") == "id:asc" for p in pages)
    finally:
        app.close()
