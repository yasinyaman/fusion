"""Telecom: billing (BSS) and the network side, kept in separate databases."""

from __future__ import annotations

from scenarios.synth.core import Database, Table, city, tpl, weighted

PLANS = Table(
    name="plans",
    base=40,
    floor=8,
    primary_key=("plan_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS plan_id,
               pick(i, 'pl_n', ['Platin', 'Altın', 'Gümüş', 'Genç', 'Kurumsal', 'Faturasız'])
                   || ' ' || CAST(i AS VARCHAR) AS plan_name,
               <kind> AS plan_type,
               CAST(round(90 + 900 * u(i, 'pl_fee'), 2) AS DECIMAL(8, 2)) AS monthly_fee,
               CAST(pick(i, 'pl_gb', [2, 5, 10, 20, 40, 100]) AS INTEGER) AS data_gb,
               CAST(pick(i, 'pl_min', [250, 500, 1000, 2000, 5000]) AS INTEGER) AS voice_min
        FROM range(1, {n} + 1) t(i)
        """,
        kind=weighted(
            "u(i, 'pl_ty')", [("postpaid", 0.55), ("prepaid", 0.35), ("corporate", 0.10)]
        ),
    ),
)

SUBSCRIBERS = Table(
    name="subscribers",
    base=500_000,
    floor=1_500,
    primary_key=("subscriber_id",),
    indexes=(("plan_id",), ("status",), ("city",)),
    select=tpl(
        """
        SELECT subscriber_id,
               '905' || lpad(CAST(300000000 + subscriber_id AS VARCHAR), 9, '0') AS msisdn,
               CAST(skew(subscriber_id, 'sb_pl', {plans}, 1.8) AS INTEGER) AS plan_id,
               <city> AS city,
               DATE '2012-01-01' + CAST(floor(u(subscriber_id, 'sb_act') * 5000) AS INTEGER)
                   AS activation_date,
               status,
               CASE WHEN status = 'churned' THEN day_at(subscriber_id, 'sb_ch') END AS churn_date,
               <segment> AS segment,
               CAST(1950 + hash(subscriber_id, 'sb_by') % 55 AS SMALLINT) AS birth_year
        FROM (
            SELECT CAST(i AS BIGINT) AS subscriber_id, <status> AS status
            FROM range(1, {n} + 1) t(i)
        )
        """,
        city=city("subscriber_id", "sb_city"),
        status=weighted(
            "u(i, 'sb_st')", [("active", 0.86), ("churned", 0.10), ("suspended", 0.04)]
        ),
        segment=weighted(
            "u(subscriber_id, 'sb_seg')", [("consumer", 0.82), ("sme", 0.13), ("corporate", 0.05)]
        ),
    ),
)

# One invoice per subscriber per month, walking back from October 2026.
INVOICES = Table(
    name="invoices",
    base=3_000_000,
    floor=9_000,
    primary_key=("invoice_id",),
    indexes=(("subscriber_id",), ("billing_month",), ("status",)),
    select=tpl(
        """
        SELECT invoice_id,
               subscriber_id,
               billing_month,
               amount_due,
               CAST(CASE WHEN u(invoice_id, 'iv_ov') < 0.25
                         THEN round(u(invoice_id, 'iv_ov2') * 120, 2) ELSE 0 END
                    AS DECIMAL(10, 2)) AS overage_fee,
               CAST(CASE status WHEN 'paid' THEN amount_due
                                WHEN 'partial' THEN round(amount_due * u(invoice_id, 'iv_pp'), 2)
                                ELSE 0 END AS DECIMAL(10, 2)) AS amount_paid,
               CASE WHEN status = 'unpaid' THEN NULL
                    ELSE billing_month + CAST(20 + hash(invoice_id, 'iv_pd') % 25 AS INTEGER)
               END AS paid_on,
               status
        FROM (
            SELECT CAST(i AS BIGINT) AS invoice_id,
                   CAST(1 + (i - 1) % {subscribers} AS BIGINT) AS subscriber_id,
                   CAST(DATE '2026-10-01' - to_months(CAST((i - 1) // {subscribers} AS INTEGER))
                        AS DATE) AS billing_month,
                   CAST(round(80 + lognorm(i, 'iv_amt', 5.0, 0.6), 2) AS DECIMAL(10, 2))
                       AS amount_due,
                   <status> AS status
            FROM range(1, {n} + 1) t(i)
        )
        """,
        status=weighted("u(i, 'iv_st')", [("paid", 0.88), ("unpaid", 0.08), ("partial", 0.04)]),
    ),
)

CELL_TOWERS = Table(
    name="cell_towers",
    base=5_000,
    floor=100,
    primary_key=("tower_id",),
    select=tpl(
        """
        SELECT CAST(i AS INTEGER) AS tower_id,
               'SITE-' || lpad(CAST(i AS VARCHAR), 5, '0') AS site_code,
               <city> AS city,
               round(36 + 6 * u(i, 'tw_lat'), 5) AS lat,
               round(26 + 19 * u(i, 'tw_lon'), 5) AS lon,
               <tech> AS technology,
               CAST(40 + hash(i, 'tw_cap') % 400 AS INTEGER) AS capacity_erl
        FROM range(1, {n} + 1) t(i)
        """,
        city=city("i", "tw_city"),
        tech=weighted("u(i, 'tw_t')", [("4G", 0.60), ("5G", 0.25), ("3G", 0.15)]),
    ),
)

# Towers whose id divides by 97 drop ten times as many calls as the rest.
CDRS = Table(
    name="cdrs",
    base=10_000_000,
    floor=40_000,
    primary_key=("cdr_id",),
    indexes=(("call_date",), ("subscriber_id",), ("tower_id",)),
    description="Call detail records: the largest table in the set.",
    select=tpl(
        """
        SELECT cdr_id,
               subscriber_id,
               tower_id,
               started_at,
               CAST(started_at AS DATE) AS call_date,
               call_type,
               CAST(CASE call_type
                        WHEN 'voice' THEN 1 + floor(lognorm(cdr_id, 'cd_du', 4.3, 1.1))
                        WHEN 'data' THEN floor(u(cdr_id, 'cd_dd') * 3600)
                        ELSE 0 END AS INTEGER) AS duration_sec,
               CASE WHEN call_type = 'data' THEN round(lognorm(cdr_id, 'cd_mb', 3.0, 1.4), 3) END
                   AS data_mb,
               CAST(round(0.05 + 2.4 * pow(u(cdr_id, 'cd_chg'), 3), 4) AS DECIMAL(10, 4)) AS charge,
               u(cdr_id, 'cd_ro') < 0.02 AS roaming,
               (call_type = 'voice' AND u(cdr_id, 'cd_dr')
                    < CASE WHEN tower_id % 97 = 0 THEN 0.12 ELSE 0.012 END) AS dropped,
               CASE WHEN call_type = 'voice'
                    THEN pick(cdr_id, 'cd_pf', ['90532', '90542', '90505', '90212', '90312',
                                                '49', '44', '1']) END AS destination_prefix
        FROM (
            SELECT CAST(i AS BIGINT) AS cdr_id,
                   skew(i, 'cd_sub', {telco_bss__subscribers}, 1.5) AS subscriber_id,
                   CAST(skew(i, 'cd_tw', {cell_towers}, 2.0) AS INTEGER) AS tower_id,
                   ts_at(i, 'cd_ts') AS started_at,
                   <kind> AS call_type
            FROM range(1, {n} + 1) t(i)
        )
        """,
        kind=weighted("u(i, 'cd_ty')", [("data", 0.55), ("voice", 0.33), ("sms", 0.12)]),
    ),
)

NETWORK_EVENTS = Table(
    name="network_events",
    base=2_000_000,
    floor=8_000,
    primary_key=("event_id",),
    indexes=(("tower_id",), ("event_ts",)),
    select=tpl(
        """
        SELECT CAST(i AS BIGINT) AS event_id,
               CAST(skew(i, 'ne_tw', {cell_towers}, 2.0) AS INTEGER) AS tower_id,
               CAST(ts_at(i, 'ne_ts') AS TIMESTAMP WITH TIME ZONE) AS event_ts,
               <kind> AS event_type,
               CAST(1 + floor(pow(u(i, 'ne_sev'), 2) * 5) AS SMALLINT) AS severity,
               CAST(round(100 * u(i, 'ne_kpi'), 2) AS REAL) AS kpi_value,
               u(i, 'ne_cl') < 0.90 AS cleared
        FROM range(1, {n} + 1) t(i)
        """,
        kind=weighted(
            "u(i, 'ne_ty')",
            [("handover_fail", 0.40), ("congestion", 0.30), ("alarm", 0.20), ("maintenance", 0.10)],
        ),
    ),
)

DATABASES = (
    Database(
        name="telco_bss",
        sector="telecom",
        engine="postgres",
        pushdown=False,
        description="Billing and subscriber management.",
        tables=(PLANS, SUBSCRIBERS, INVOICES),
    ),
    Database(
        name="telco_network",
        sector="telecom",
        engine="postgres",
        pushdown=False,
        description="Network: towers, call detail records, network events.",
        tables=(CELL_TOWERS, CDRS, NETWORK_EVENTS),
    ),
)
