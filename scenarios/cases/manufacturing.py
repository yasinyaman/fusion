"""Manufacturing: the MES and an eight-million-row telemetry historian."""

from __future__ import annotations

from scenarios.harness import Ctx, scenario

READINGS = "factory_iot.sensor_readings"
CHECKS = "factory_mes.quality_checks"


def _machines(ctx: Ctx) -> list[int]:
    """The machines seeded to run hot (id % 37 = 5), plus two that do not."""
    count = ctx.rows("factory_mes.machines")
    return [i for i in range(1, count + 1) if i % 37 == 5][:4] + [1, 2]


@scenario(
    "M01",
    sector="manufacturing",
    title="Sensor anomalies: rolling z-scores on a few machines' full history",
    sources=("factory_mes", "factory_iot"),
    capabilities=("refusal", "load_table", "window", "float4", "federation", "semi-join"),
)
def anomalies(ctx: Ctx) -> None:
    """Maintenance asks which machines show temperature excursions. A rolling z-score
    over the whole historian is refused; the history of the machines in question is
    loaded by id and the window runs over that, and it has to find the seeded ones."""
    machines = _machines(ctx)
    ids = ", ".join(str(m) for m in machines)

    def zscores(table: str, where: str = "") -> str:
        return f"""
            WITH w AS (
                SELECT machine_id, read_at, vibration_mm_s AS v, temperature,
                       AVG(vibration_mm_s) OVER r AS mean, STDDEV_SAMP(vibration_mm_s) OVER r AS sd
                FROM {table} {where}
                -- The baseline ends a dozen readings back, so an excursion in progress
                -- does not raise its own bar.
                WINDOW r AS (PARTITION BY machine_id ORDER BY read_at
                             ROWS BETWEEN 200 PRECEDING AND 13 PRECEDING)
            )
            SELECT machine_id, COUNT(*) AS readings,
                   COUNT(*) FILTER (WHERE (v - mean) / sd > 6) AS excursions,
                   AVG(temperature) FILTER (WHERE (v - mean) / sd > 6) AS temp_during
            FROM w GROUP BY machine_id ORDER BY machine_id"""

    ctx.refused(zscores(READINGS), "the window over the whole historian is refused")
    loaded = ctx.tool(
        "load_table",
        {
            "table": READINGS,
            "where": f"machine_id IN ({ids})",
            "columns": ["machine_id", "read_at", "temperature", "vibration_mm_s"],
        },
        f"load the history of machines {ids}",
    )
    result = ctx.sql(
        zscores(loaded.get("slice_table", READINGS)),
        "vibration excursions per machine on the slice",
        ordered=True,
        oracle_sql=zscores(READINGS, f"WHERE machine_id IN ({ids})"),
        max_moved=0,
        tolerance=1e-6,
    )
    if result is not None and result.rows:
        hot = {r[0]: r[2] for r in result.rows if r[0] % 37 == 5}
        cold = {r[0]: r[2] for r in result.rows if r[0] % 37 != 5}
        ctx.check(
            "the seeded machines stand out, the others are nearly clean",
            min(hot.values(), default=0) > 20 * max(cold.values(), default=0),
            f"seeded {hot}, others {cold}",
        )
    ctx.sql(
        "SELECT m.plant, m.machine_type, COUNT(*) AS readings, AVG(s.temperature) AS temp, "
        "MAX(s.vibration_mm_s) AS worst_vibration "
        f"FROM factory_mes.machines m JOIN {READINGS} s ON s.machine_id = m.machine_id "
        "WHERE m.plant = 'Manisa' AND s.read_at >= '2026-10-01 00:00:00+00:00' "
        "GROUP BY m.plant, m.machine_type ORDER BY m.machine_type",
        "October telemetry of one plant (machines and readings in two databases)",
        ordered=True,
        tolerance=1e-6,
        actions={READINGS: ("load_slice", "semi_join")},
    )
    kept = ctx.pushed(
        f"SELECT COUNT(*) FROM {READINGS} WHERE temperature > 80.5 "
        "AND read_at >= '2026-10-01 00:00:00+00:00'",
        READINGS,
    )
    ctx.check(
        "a float4 condition stays local", ("temperature", "gt") not in kept["predicates"], str(kept)
    )
    ctx.sql(
        f"SELECT machine_id, COUNT(*) AS hot FROM {READINGS} WHERE temperature > 80.5 "
        "AND read_at >= '2026-10-01 00:00:00+00:00' GROUP BY machine_id "
        "ORDER BY hot DESC, machine_id LIMIT 10",
        "hot readings this month",
        ordered=True,
    )
    ctx.sql(
        f"SELECT quality_flag, COUNT(*) AS n, COUNT(spindle_rpm) AS with_rpm FROM {READINGS} "
        "WHERE quality_flag >= 1 GROUP BY quality_flag ORDER BY quality_flag",
        "flagged readings and how many lack an rpm",
        ordered=True,
        actions={READINGS: "load_slice"},
        max_moved=ctx.rows(READINGS) // 10,
    )


@scenario(
    "M02",
    sector="manufacturing",
    title="Process capability (Cpk) and scrap by line for one month",
    sources=("factory_mes",),
    capabilities=("slice", "statistics", "join"),
)
def capability(ctx: Ctx) -> None:
    """Quality computes Cpk per line from October's measurements and the scrap rate per
    shift from the work orders. Standard deviations over tens of thousands of floats
    must agree with the source to the last few digits."""
    ctx.sql(
        "SELECT m.line, COUNT(*) AS checks, AVG(q.measured) AS mean, "
        "STDDEV_SAMP(q.measured) AS sd, "
        "LEAST(MAX(q.usl) - AVG(q.measured), AVG(q.measured) - MIN(q.lsl)) "
        "/ (3 * STDDEV_SAMP(q.measured)) AS cpk, AVG(CASE WHEN q.passed THEN 1.0 ELSE 0 END) "
        f"AS first_pass_yield FROM {CHECKS} q "
        "JOIN factory_mes.work_orders w ON w.wo_id = q.wo_id "
        "JOIN factory_mes.machines m ON m.machine_id = w.machine_id "
        "WHERE q.checked_at >= '2026-10-01' GROUP BY m.line ORDER BY m.line",
        "Cpk and first-pass yield per line in October",
        ordered=True,
        tolerance=1e-9,
        actions={CHECKS: "load_slice"},
    )
    ctx.sql(
        "SELECT shift, SUM(planned_qty) AS planned, SUM(good_qty) AS good, "
        "SUM(scrap_qty) AS scrap, SUM(scrap_qty) / SUM(planned_qty) AS scrap_rate "
        "FROM factory_mes.work_orders WHERE started_at >= '2026-10-01' "
        "GROUP BY shift ORDER BY shift",
        "scrap rate per shift",
        ordered=True,
        tolerance=1e-12,
    )
    ctx.sql(
        f"SELECT w.wo_id % 7 = 0 AS wide_spread, STDDEV_SAMP(q.measured) AS sd FROM {CHECKS} q "
        "JOIN factory_mes.work_orders w ON w.wo_id = q.wo_id "
        "WHERE q.checked_at >= '2026-10-01' GROUP BY 1 ORDER BY 1",
        "the seeded wide-spread orders, from the slice already held",
        ordered=True,
        tolerance=1e-9,
        actions={CHECKS: "reuse"},
        max_moved=0,
    )


@scenario(
    "M03",
    sector="manufacturing",
    title="Downtime Pareto across the MES and the historian",
    sources=("factory_mes", "factory_iot"),
    capabilities=("federation", "window", "asof", "limit"),
)
def downtime(ctx: Ctx) -> None:
    """The plant manager wants the reasons behind unplanned downtime, as a Pareto with a
    cumulative share, and the temperature just before each stop. The first is a window
    over two small tables; the second is an ASOF join to the historian."""
    pareto = """
        WITH d AS (
            SELECT m.plant, e.reason_code, SUM(e.duration_min) AS minutes
            FROM factory_iot.downtime_events e
            JOIN factory_mes.machines m ON m.machine_id = e.machine_id
            WHERE NOT e.planned GROUP BY m.plant, e.reason_code
        )
        SELECT plant, reason_code, minutes,
               SUM(minutes) OVER (PARTITION BY plant ORDER BY minutes DESC, reason_code)
                   / SUM(minutes) OVER (PARTITION BY plant) AS cumulative_share
        FROM d ORDER BY plant, minutes DESC, reason_code"""
    ctx.sql(pareto, "unplanned downtime Pareto per plant", ordered=True, tolerance=1e-12)

    def before_stop(readings: str, where: str = "") -> str:
        return f"""
            SELECT e.reason_code, COUNT(*) AS stops, AVG(r.temperature) AS temp_before
            FROM factory_iot.downtime_events e
            ASOF JOIN (SELECT * FROM {readings} {where}) r
              ON r.machine_id = e.machine_id AND r.read_at <= e.started_at
            WHERE e.machine_id = 5 GROUP BY e.reason_code ORDER BY e.reason_code"""

    ctx.limit(
        "SELECT e.reason_code, COUNT(*) AS stops, AVG(r.temperature) AS temp_before "
        f"FROM factory_iot.downtime_events e ASOF JOIN {READINGS} r "
        "ON r.machine_id = e.machine_id AND r.read_at <= e.started_at "
        "WHERE e.machine_id = 5 GROUP BY e.reason_code ORDER BY e.reason_code",
        "an ASOF join straight to the historian",
        ordered=True,
    )
    loaded = ctx.tool(
        "load_table",
        {
            "table": READINGS,
            "where": "machine_id = 5",
            "columns": ["machine_id", "read_at", "temperature"],
        },
        "load one machine's history",
    )
    ctx.sql(
        before_stop(loaded.get("slice_table", READINGS)),
        "temperature just before each stop, on the slice",
        ordered=True,
        tolerance=1e-6,
        oracle_sql=before_stop(READINGS, "WHERE machine_id = 5"),
        max_moved=0,
    )


@scenario(
    "M04",
    sector="manufacturing",
    title="A live line: incremental refresh, updates, deletes and the cache",
    sources=(),
    capabilities=(
        "incremental-refresh",
        "upsert",
        "cache-invalidation",
        "scheduler",
        "materialized-view",
    ),
    settings={
        "refresh_config": '{"line.readings": {"watermark_column": "updated_at", '
        '"key_columns": ["reading_id"]}, "line.counters": '
        '{"watermark_column": "tick"}}'
    },
    modes=("inproc",),
)
def live_line(ctx: Ctx) -> None:
    """A production line keeps writing. Readings carry an update time and a key, so a
    refresh should fetch only what changed and replace updated rows; an append-only
    counter table has a watermark and no key. Cached answers from before a refresh
    must not be served after it."""
    n = max(2_000, int(200_000 * ctx.scale))
    source = ctx.scratch(
        "line",
        {
            "readings": f"""SELECT CAST(i AS BIGINT) AS reading_id,
                               CAST(1 + i % 12 AS INTEGER) AS machine_id,
                               TIMESTAMP '2026-10-01' + to_seconds(CAST(i * 30 AS BIGINT))
                                   AS updated_at,
                               round(50 + (hash(i, 'lt') % 1000) / 50.0, 2) AS temperature
                        FROM range(1, {n} + 1) t(i)""",
            "counters": f"""SELECT CAST(i AS BIGINT) AS tick, CAST(i % 5 AS INTEGER) AS station,
                               CAST(hash(i, 'lc') % 9 AS INTEGER) AS units
                        FROM range(1, {n // 10} + 1) t(i)""",
        },
    )
    per_machine = (
        "SELECT machine_id, COUNT(*) AS n, SUM(temperature) AS total, "
        "MAX(updated_at) AS latest FROM line.readings GROUP BY machine_id "
        "ORDER BY machine_id"
    )
    per_station = (
        "SELECT station, COUNT(*) AS n, SUM(units) AS units FROM line.counters "
        "GROUP BY station ORDER BY station"
    )
    ctx.sql(
        per_machine,
        "initial load of the readings",
        ordered=True,
        actions={"line.readings": "load_full"},
    )
    ctx.sql(per_station, "initial load of the counters", ordered=True)
    ctx.sql(per_machine, "served from the cache", ordered=True, from_cache=True)
    ctx.tool(
        "create_view",
        {
            "name": "line_hourly",
            "sql": (
                "SELECT machine_id, date_trunc('hour', updated_at) AS hour, "
                "AVG(temperature) AS temp FROM line.readings GROUP BY 1, 2"
            ),
        },
        "a view over the readings",
    )
    ctx.sql(
        per_machine,
        "creating a view clears the cache (conservative, still right)",
        ordered=True,
        from_cache=False,
    )

    added = max(50, n // 100)
    ctx.mutate(f"""INSERT INTO line.main.readings
                   SELECT {n} + i, CAST(1 + i % 12 AS INTEGER),
                          TIMESTAMP '2026-10-01' + to_seconds(CAST(({n} + i) * 30 AS BIGINT)),
                          99.5 FROM range(1, {added} + 1) t(i)""")
    ctx.mutate(f"""UPDATE line.main.readings SET temperature = temperature + 10,
                   updated_at = TIMESTAMP '2026-10-01' + to_seconds(CAST(({n} + {added} + 1)
                   * 30 AS BIGINT)) WHERE reading_id % 97 = 0 AND reading_id <= {n}""")
    ctx.mutate(
        f"INSERT INTO line.main.counters SELECT {n // 10} + i, CAST(i % 5 AS INTEGER), 7 "
        f"FROM range(1, {added} + 1) t(i)"
    )
    updated = ctx.scalar(
        f"SELECT count(*) FROM line.readings WHERE reading_id % 97 = 0 AND reading_id <= {n}"
    )

    ctx.app.sources.start_auto_refresh(interval=300)
    before = source.stats.rows_sent
    ctx.clock.advance(301)
    ctx.scheduler.tick()
    fetched = source.stats.rows_sent - before
    ctx.check(
        "the refresh fetched what changed, not the tables",
        fetched <= added * 2 + updated + 10,
        f"{fetched:,} rows fetched for {added * 2:,} new and {updated:,} updated",
    )
    ctx.sql(
        per_machine,
        "after the refresh: new and updated readings, no stale cache",
        ordered=True,
        from_cache=False,
    )
    ctx.sql(per_station, "after the refresh: the appended counters", ordered=True, from_cache=False)
    ctx.sql(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT reading_id) AS keys FROM line.readings",
        "an updated row replaced its old copy instead of doubling",
    )
    ctx.tool("refresh_view", {"name": "line_hourly"}, "refresh the view")
    ctx.sql(
        "SELECT machine_id, COUNT(*) AS hours, AVG(temp) AS temp FROM mv_line_hourly "
        "GROUP BY machine_id ORDER BY machine_id",
        "the view reflects the new data",
        ordered=True,
        tolerance=1e-9,
        oracle_sql="SELECT machine_id, COUNT(*), AVG(temp) FROM (SELECT machine_id, "
        "date_trunc('hour', updated_at) AS hour, AVG(temperature) AS temp "
        "FROM line.readings GROUP BY 1, 2) GROUP BY 1 ORDER BY 1",
    )

    ctx.mutate("DELETE FROM line.main.readings WHERE machine_id = 3")
    ctx.clock.advance(301)
    ctx.scheduler.tick()
    held = ctx.app.query.sql(
        "SELECT COUNT(*) FROM line.readings WHERE machine_id = 3", use_cache=False
    ).rows[0][0]
    ctx.note(
        "a delete at the source is invisible to a watermark refresh",
        f"machine 3 still has {held:,} rows in the store, 0 at the source",
    )
    ctx.app.sources.unload("line.readings")
    ctx.sql(per_machine, "unloading and reading again brings the delete in", ordered=True)
