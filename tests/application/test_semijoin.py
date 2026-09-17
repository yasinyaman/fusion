"""Key passing: fetch a huge table by the keys a small one holds."""

from dataclasses import replace

import pytest

from fusion.bootstrap import build_app
from fusion.domain.errors import QueryError
from fusion.domain.models import TableRef
from fusion.domain.policy import SemiJoinSpec, TargetPlan
from fusion.domain.slices import SliceSpec

EVENTS = [{"id": i, "user_id": (i % 4) + 1, "kind": "click"} for i in range(1, 21)]
USERS = [{"id": i, "segment": "premium" if i < 3 else "basic"} for i in range(1, 6)]
TABLES = {"users": USERS, "events": EVENTS}

BIG = TableRef("db", "events")
SMALL = TableRef("db", "users")


@pytest.fixture
def app(settings, scheduler, factory):
    """``events`` is far too big to load whole; ``users`` is tiny."""
    a = build_app(
        replace(settings, full_load_max_rows=100, slice_max_rows=1000),
        scheduler=scheduler,
        source_factory=factory,
    )
    a.sources.connect(
        "db", {"type": "fake", "tables": TABLES, "row_estimates": {"events": 9_000_000}}
    )
    yield a
    a.close()


def _plan(app, spec=None, driver_key="id", target_key="user_id"):
    app.sources.ensure_slices([TargetPlan(SMALL, SliceSpec.FULL, "load_full")])
    return TargetPlan(
        BIG,
        spec or SliceSpec.FULL,
        "semi_join",
        semi_join=SemiJoinSpec(SMALL, "db.users", driver_key, target_key),
    )


class TestSemiJoin:
    def test_fetches_only_rows_matching_a_driver_key(self, app, factory):
        target = _plan(app)
        mapping = app.sources.ensure_slices([target])
        table = mapping[BIG]
        assert app.store.count(table) == 20  # every user_id 1..4 is in users
        loaded = app.catalog.slices_of(BIG)[0]
        assert loaded.derived_from == "semijoin:db.users.id"
        assert loaded.spec.predicates[-1].column == "user_id"
        assert set(loaded.spec.predicates[-1].value) == {1, 2, 3, 4, 5}

    def test_only_the_keys_that_exist_are_requested(self, app, factory):
        app.sources.ensure_slices([_plan(app)])
        calls = factory.sources["db"].calls_named("fetch_slice")
        events = [c for c in calls if c[1] == "events"]
        assert events, "the big table was never read"
        keys = events[0][2].predicates[-1]
        assert keys.op == "in" and set(keys.value) <= {1, 2, 3, 4, 5}

    def test_a_driver_with_fewer_keys_narrows_the_result(self, app, factory):
        factory.sources["db"].set_table("users", [{"id": 2, "segment": "premium"}])
        target = _plan(app)
        table = app.sources.ensure_slices([target])[BIG]
        rows = app.store.execute(f"SELECT DISTINCT user_id FROM {table}").rows
        assert rows == [(2,)]
        assert app.store.count(table) == 5

    def test_keys_are_sent_in_chunks(self, app, factory):
        app.sources._policy = replace(app.sources._policy, in_chunk_size=2)
        app.sources._semi_join_executor._policy = app.sources._policy
        app.sources.ensure_slices([_plan(app)])
        events = [c for c in factory.sources["db"].calls_named("fetch_slice") if c[1] == "events"]
        assert len(events) == 3  # 5 keys in chunks of 2
        assert all(len(c[2].predicates[-1].value) <= 2 for c in events)
        assert app.store.count(app.catalog.slices_of(BIG)[0].table_name) == 20

    def test_null_keys_are_skipped(self, app, factory):
        factory.sources["db"].set_table(
            "users", [{"id": None, "segment": "x"}, {"id": 1, "segment": "y"}]
        )
        app.sources.ensure_slices([_plan(app)])
        keys = app.catalog.slices_of(BIG)[0].spec.predicates[-1].value
        assert set(keys) == {1}

    def test_too_many_keys_is_refused_with_advice(self, app, factory):
        factory.sources["db"].set_table("users", [{"id": i, "segment": "x"} for i in range(50)])
        app.sources._policy = replace(app.sources._policy, semi_join_max_keys=10)
        app.sources._semi_join_executor._policy = app.sources._policy
        with pytest.raises(QueryError) as error:
            app.sources.ensure_slices([_plan(app)])
        assert "semi_join_max_keys=10" in str(error.value)
        assert "FUSION_SEMI_JOIN_MAX_KEYS" in str(error.value)

    def test_an_empty_driver_still_leaves_a_usable_table(self, app, factory):
        factory.sources["db"].set_table("users", [])
        target = _plan(app)
        table = app.sources.ensure_slices([target])[BIG]
        assert app.store.count(table) == 0
        assert {c.name for c in app.store.describe(table)} == {"id", "user_id", "kind"}

    def test_a_projection_is_kept_alongside_the_keys(self, app):
        target = _plan(app, SliceSpec(columns=frozenset({"id", "user_id"})))
        table = app.sources.ensure_slices([target])[BIG]
        assert {c.name for c in app.store.describe(table)} == {"id", "user_id"}

    def test_the_same_join_reuses_the_slice(self, app, factory):
        target = _plan(app)
        first = app.sources.ensure_slices([target])[BIG]
        before = len(factory.sources["db"].calls_named("fetch_slice"))
        second = app.sources.ensure_slices([target])[BIG]
        assert second == first
        assert len(factory.sources["db"].calls_named("fetch_slice")) == before

    def test_a_failed_fetch_leaves_no_staging_table(self, app, factory):
        target = _plan(app)

        def boom(table, spec, max_rows=None):
            if table == "events":
                raise QueryError("source exploded")
            raise AssertionError(table)

        factory.sources["db"].fetch_slice = boom
        with pytest.raises(QueryError, match="exploded"):
            app.sources.ensure_slices([target])
        with pytest.raises(QueryError):
            app.store.count(f"{target.table_name}__tmp")

    def test_an_invalid_join_key_is_refused(self, app):
        target = _plan(app, driver_key="id; DROP TABLE users")
        with pytest.raises(QueryError, match="Invalid join key"):
            app.sources.ensure_slices([target])


class TestSemiJoinThroughTheQueryPipeline:
    def test_a_join_loads_only_the_matching_rows(self, app, factory):
        result = app.query.sql(
            "SELECT u.segment, COUNT(*) AS n FROM db.users u "
            "JOIN db.events e ON u.id = e.user_id WHERE u.segment = 'premium' "
            "GROUP BY u.segment"
        )
        assert result.to_records() == [{"segment": "premium", "n": 10}]
        assert app.catalog.is_loaded("db.users")
        assert not app.catalog.is_loaded("db.events")
        slices = app.catalog.slices_of(BIG)
        assert len(slices) == 1 and slices[0].derived_from.startswith("semijoin:")

    def test_without_a_join_the_same_table_is_refused(self, app):
        with pytest.raises(QueryError, match="Refusing to load db.events"):
            app.query.sql("SELECT * FROM db.events")
