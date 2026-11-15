"""Manufacturing: the MES (orders, quality) and the machine telemetry historian.

Sensor readings are a real time series: every machine reports on a fixed
cadence, so a window over one machine is dense and ordered. Machines whose id
leaves 5 when divided by 37 run hot and shake for a dozen readings in every
four hundred: the excursions the anomaly scenario has to find.
"""

from __future__ import annotations

from scenarios.synth.core import DAYS, Database, Table, tpl, weighted

PLANTS = ["Gebze", "Bursa", "Manisa", "Eskişehir"]

MACHINES = Table(
    name="machines",
    base=600,
    floor=40,
    primary_key=("machine_id",),
    select="""
        SELECT CAST(i AS INTEGER) AS machine_id,
               'MC-' || lpad(CAST(i AS VARCHAR), 4, '0') AS machine_code,
               'L' || lpad(CAST(1 + (i - 1) % 24 AS VARCHAR), 2, '0') AS line,
               ['Gebze', 'Bursa', 'Manisa', 'Eskişehir'][1 + (i - 1) % 4] AS plant,
               pick(i, 'mc_t', ['pres', 'cnc', 'kaynak', 'boya', 'montaj']) AS machine_type,
               DATE '2008-01-01' + CAST(floor(u(i, 'mc_in') * 6000) AS INTEGER) AS installed_on,
               round(8 + u(i, 'mc_cy') * 52, 1) AS nominal_cycle_sec
        FROM range(1, {n} + 1) t(i)
    """,
)

WORK_ORDERS = Table(
    name="work_orders",
    base=400_000,
    floor=2_000,
    primary_key=("wo_id",),
    indexes=(("machine_id",), ("started_at",)),
    select="""
        SELECT wo_id,
               machine_id,
               'P-' || lpad(CAST(1 + hash(wo_id, 'wo_pc') % 400 AS VARCHAR), 4, '0')
                   AS product_code,
               planned_qty,
               CAST(floor(planned_qty * (0.86 + 0.13 * u(wo_id, 'wo_good'))) AS INTEGER)
                   AS good_qty,
               CAST(floor(planned_qty * pow(u(wo_id, 'wo_scrap'), 4) * 0.08) AS INTEGER)
                   AS scrap_qty,
               started_at,
               started_at + to_minutes(CAST(30 + hash(wo_id, 'wo_len') % 450 AS BIGINT))
                   AS finished_at,
               CASE WHEN hour(started_at) < 8 THEN 'C'
                    WHEN hour(started_at) < 16 THEN 'A' ELSE 'B' END AS shift
        FROM (
            SELECT CAST(i AS BIGINT) AS wo_id,
                   CAST(1 + hash(i, 'wo_m') % {machines} AS INTEGER) AS machine_id,
                   CAST(100 + hash(i, 'wo_q') % 1900 AS INTEGER) AS planned_qty,
                   ts_at(i, 'wo_ts') AS started_at
            FROM range(1, {n} + 1) t(i)
        )
    """,
)

# Every seventh work order runs with two and a half times the spread, which
# is what pulls its Cpk under 1.
QUALITY_CHECKS = Table(
    name="quality_checks",
    base=1_200_000,
    floor=6_000,
    primary_key=("check_id",),
    indexes=(("wo_id",), ("checked_at",)),
    select="""
        SELECT check_id,
               wo_id,
               pick(check_id, 'qc_c', ['çap', 'tork', 'kalınlık', 'sertlik', 'yüzey'])
                   AS characteristic,
               measured,
               9.85 AS lsl,
               10.15 AS usl,
               measured BETWEEN 9.85 AND 10.15 AS passed,
               ts_at(check_id, 'qc_ts') AS checked_at
        FROM (
            SELECT check_id, wo_id,
                   round(10 + gauss(check_id, 'qc_m') * 0.05
                            * CASE WHEN wo_id % 7 = 0 THEN 2.5 ELSE 1 END, 4) AS measured
            FROM (
                SELECT CAST(i AS BIGINT) AS check_id,
                       CAST(1 + hash(i, 'qc_wo') % {work_orders} AS BIGINT) AS wo_id
                FROM range(1, {n} + 1) t(i)
            )
        )
    """,
)

SENSOR_READINGS = Table(
    name="sensor_readings",
    base=8_000_000,
    floor=32_000,
    primary_key=("reading_id",),
    indexes=(("machine_id", "read_at"), ("read_at",)),
    description="Telemetry: zoned timestamps, single-precision temperature and vibration.",
    select=tpl(
        """
        SELECT reading_id,
               machine_id,
               read_at,
               CAST(55 + 12 * gauss(reading_id, 'sr_t') + CASE WHEN anomaly THEN 25 ELSE 0 END
                    AS REAL) AS temperature,
               CAST(2.2 + 0.6 * abs(gauss(reading_id, 'sr_v'))
                    + CASE WHEN anomaly THEN 6 ELSE 0 END AS REAL) AS vibration_mm_s,
               round(6 + gauss(reading_id, 'sr_p') * 0.4, 3) AS pressure_bar,
               round(18 + 9 * u(reading_id, 'sr_kw'), 3) AS power_kw,
               CASE WHEN u(reading_id, 'sr_rn') < 0.07 THEN NULL
                    ELSE CAST(900 + floor(u(reading_id, 'sr_r') * 2400) AS INTEGER) END
                   AS spindle_rpm,
               CAST(CASE WHEN anomaly THEN 2
                         WHEN u(reading_id, 'sr_q') < 0.01 THEN 1 ELSE 0 END AS SMALLINT)
                   AS quality_flag
        FROM (
            SELECT reading_id,
                   machine_id,
                   TIMESTAMPTZ '2024-11-01 00:00:00+00' + to_seconds(CAST(floor(
                       step * (<seconds> / greatest(1, {n} // {factory_mes__machines})))
                       AS BIGINT)) AS read_at,
                   (machine_id % 37 = 5 AND step % 400 < 12) AS anomaly
            FROM (
                SELECT CAST(i AS BIGINT) AS reading_id,
                       CAST(1 + (i - 1) % {factory_mes__machines} AS INTEGER) AS machine_id,
                       (i - 1) // {factory_mes__machines} AS step
                FROM range(1, {n} + 1) t(i)
            )
        )
        """,
        seconds=f"{DAYS * 86400}.0",
    ),
)

DOWNTIME_EVENTS = Table(
    name="downtime_events",
    base=80_000,
    floor=600,
    primary_key=("event_id",),
    indexes=(("machine_id",), ("started_at",)),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS event_id,
               CAST(1 + hash(i, 'dt_m') % {factory_mes__machines} AS INTEGER) AS machine_id,
               ts_at(i, 'dt_ts') AS started_at,
               CAST(1 + floor(lognorm(i, 'dt_d', 3.2, 1.0)) AS INTEGER) AS duration_min,
               <reason> AS reason_code,
               u(i, 'dt_pl') < 0.30 AS planned
        FROM range(1, {n} + 1) t(i)
        """,
        reason=weighted(
            "u(i, 'dt_r')",
            [
                ("arıza", 0.34),
                ("kalıp değişimi", 0.22),
                ("malzeme bekleme", 0.18),
                ("bakım", 0.12),
                ("ayar", 0.10),
                ("enerji kesintisi", 0.04),
            ],
        ),
    ),
)

DATABASES = (
    Database(
        name="factory_mes",
        sector="manufacturing",
        engine="postgres",
        pushdown=False,
        description="Manufacturing execution: machines, work orders, quality checks.",
        tables=(MACHINES, WORK_ORDERS, QUALITY_CHECKS),
    ),
    Database(
        name="factory_iot",
        sector="manufacturing",
        engine="postgres",
        pushdown=False,
        description="Machine telemetry and downtime.",
        tables=(SENSOR_READINGS, DOWNTIME_EVENTS),
    ),
)
