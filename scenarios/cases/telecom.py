"""Telecom: ten million call records and the billing system beside them."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from scenarios.harness import Budgets, Ctx, scenario

CDRS = "telco_network.cdrs"
INVOICES = "telco_bss.invoices"


def _day_query(day: str) -> str:
    return (
        f"SELECT call_type, COUNT(*) AS n, SUM(duration_sec) AS seconds, SUM(charge) AS "
        f"charged FROM {CDRS} WHERE call_date = '{day}' GROUP BY call_type ORDER BY call_type"
    )


@scenario(
    "T01",
    sector="telecom",
    title="Daily traffic reports under a tight memory budget",
    sources=("telco_network",),
    capabilities=("slice", "budget", "eviction", "refusal"),
    budgets=Budgets(total=120_000),
)
def daily_reports(ctx: Ctx) -> None:
    """Two weeks of daily traffic reports, each a slice of the call records, with room
    for only about a week of them. Older days must be evicted, every report must still
    be right, and a question that could never fit is refused rather than attempted."""
    budget = ctx.budget("slice_budget_rows")
    days = [(date(2026, 10, 1) + timedelta(days=n)).isoformat() for n in range(14)]
    over = 0
    for day in days:
        ctx.sql(
            _day_query(day),
            f"traffic on {day}",
            ordered=True,
            tolerance=0.0,
            actions={CDRS: "load_slice"},
        )
        over = max(over, ctx.held() - budget)
    ctx.check(
        "the store never held more than the budget",
        over <= 0,
        f"{over:,} rows over slice_budget_rows={budget:,}",
    )
    kept = len(ctx.app.catalog.slices_of(CDRS))
    ctx.check(
        "older days were evicted to make room",
        0 < kept < len(days),
        f"{kept} of {len(days)} daily slices still held",
    )
    ctx.sql(
        _day_query(days[0]),
        "the first day again: evicted, so fetched again and still right",
        ordered=True,
        tolerance=0.0,
        use_cache=False,
        actions={CDRS: "load_slice"},
    )
    ctx.sql(
        _day_query(days[-1]),
        "the last day is still held",
        ordered=True,
        tolerance=0.0,
        use_cache=False,
        actions={CDRS: "reuse"},
        max_moved=0,
    )
    ctx.refused(
        f"SELECT call_type, COUNT(*) FROM {CDRS} WHERE call_date >= '2026-10-01' "
        "GROUP BY call_type",
        "a whole month cannot fit in the budget and says so",
        contains=("Slice budget exhausted",),
    )


@scenario(
    "T02",
    sector="telecom",
    title="Dropped calls per tower in one city",
    sources=("telco_network",),
    capabilities=("semi-join", "unicode", "refusal", "driver-filter"),
)
def dropped_calls(ctx: Ctx) -> None:
    """Network operations ask which towers in Iğdır drop the most calls. The towers of
    one city are a handful of keys; their call records are fetched by key. Asking the
    same for İstanbul would pull a quarter of the table and is refused instead."""

    def per_tower(city: str, extra: str = "") -> str:
        return (
            "SELECT w.site_code, w.technology, COUNT(*) AS calls, "
            "SUM(CASE WHEN c.dropped THEN 1 ELSE 0 END) AS dropped, "
            "AVG(CASE WHEN c.dropped THEN 1.0 ELSE 0.0 END) AS drop_rate "
            f"FROM telco_network.cell_towers w JOIN {CDRS} c ON c.tower_id = w.tower_id "
            f"WHERE w.city = '{city}' AND c.call_type = 'voice' {extra} "
            "GROUP BY w.site_code, w.technology ORDER BY drop_rate DESC, w.site_code"
        )

    small = "Iğdır"
    share = ctx.scalar(
        f"SELECT count(*) FROM {CDRS} c JOIN telco_network.cell_towers w "
        f"ON w.tower_id = c.tower_id WHERE w.city = '{small}'"
    )
    result = ctx.sql(
        per_tower(small),
        f"drop rate per tower in {small}",
        ordered=True,
        max_moved=share + ctx.rows("telco_network.cell_towers") + 10,
    )
    ctx.note("how the call records were reached", str(ctx.steps[-1].actions))
    if result is not None and result.rows:
        ctx.check(
            "only that city's records were fetched",
            not ctx.inproc
            or ctx.steps[-2].moved is None
            or ctx.steps[-2].moved <= share + ctx.rows("telco_network.cell_towers") + 10,
        )
    ctx.sql(
        "SELECT w.city, COUNT(*) AS calls, AVG(CASE WHEN c.dropped THEN 1.0 ELSE 0.0 END) AS "
        f"drop_rate FROM telco_network.cell_towers w JOIN {CDRS} c ON c.tower_id = w.tower_id "
        "WHERE w.tower_id IN (97, 194, 291) GROUP BY w.city ORDER BY w.city",
        "the three towers known to be faulty",
        ordered=True,
    )
    ctx.limit(
        "SELECT w.technology, COUNT(*) AS calls FROM telco_network.cell_towers w "
        f"JOIN {CDRS} c ON c.tower_id = w.tower_id WHERE w.city = 'İstanbul' "
        "GROUP BY w.technology ORDER BY w.technology",
        "the biggest city by key: too many rows would come back",
        ordered=True,
    )
    ctx.sql(
        per_tower("İstanbul", "AND c.call_date >= '2026-10-25'"),
        "the biggest city, narrowed to a week on the call records",
        ordered=True,
        actions={CDRS: "load_slice"},
    )


@scenario(
    "T03",
    sector="telecom",
    title="Revenue assurance: billed against rated usage, across two systems",
    sources=("telco_bss", "telco_network"),
    capabilities=("federation", "two-slices", "slice-reuse", "outer-join", "limit"),
)
def revenue_assurance(ctx: Ctx) -> None:
    """October's invoices live in billing, October's rated calls in the network
    database. Joining them by subscriber needs a slice of each, half a million rows
    apiece at full scale."""
    usage = (
        "SELECT p.plan_type, COUNT(DISTINCT i.subscriber_id) AS subscribers, COUNT(*) AS calls, "
        f"SUM(c.charge) AS rated FROM {INVOICES} i "
        f"JOIN {CDRS} c ON c.subscriber_id = i.subscriber_id "
        "JOIN telco_bss.subscribers s ON s.subscriber_id = i.subscriber_id "
        "JOIN telco_bss.plans p ON p.plan_id = s.plan_id "
        "WHERE i.billing_month = '2026-10-01' AND c.call_date >= '2026-10-01' "
        "AND c.call_date <= '2026-10-31' {extra} GROUP BY p.plan_type ORDER BY p.plan_type"
    )
    ctx.sql(
        usage.format(extra=""),
        "rated usage per plan type for October's invoices",
        ordered=True,
        tolerance=0.0,
        actions={INVOICES: "load_slice", CDRS: "load_slice"},
    )
    ctx.sql(
        usage.format(extra="AND i.status = 'unpaid'"),
        "the unpaid ones: the call slice is reused, invoices need the status column",
        ordered=True,
        tolerance=0.0,
        actions={INVOICES: "load_slice", CDRS: "reuse"},
        max_moved=ctx.rows("telco_bss.subscribers"),
    )
    ctx.sql(
        "SELECT s.segment, COUNT(*) AS invoices, SUM(i.amount_due) AS due, "
        "SUM(i.amount_due - i.amount_paid) AS outstanding "
        f"FROM {INVOICES} i JOIN telco_bss.subscribers s ON s.subscriber_id = i.subscriber_id "
        "WHERE i.billing_month = '2026-10-01' GROUP BY s.segment ORDER BY s.segment",
        "outstanding balance by segment: one month of invoices, two more columns",
        ordered=True,
        tolerance=0.0,
        actions={INVOICES: "load_slice"},
        max_moved=ctx.rows("telco_bss.subscribers"),
    )
    ctx.limit(
        "SELECT COUNT(*) AS unbilled FROM telco_bss.subscribers s "
        f"LEFT JOIN {INVOICES} i ON i.subscriber_id = s.subscriber_id "
        "AND i.billing_month = '2026-10-01' WHERE i.invoice_id IS NULL AND s.status = 'active'",
        "active subscribers with no October invoice (the filter sits on the outer side)",
    )
    ctx.sql(
        "SELECT COUNT(*) AS unbilled FROM telco_bss.subscribers s "
        f"WHERE s.status = 'active' AND s.subscriber_id NOT IN (SELECT subscriber_id "
        f"FROM {_month_slice(ctx)})",
        "the same question against the invoice slice already held",
        oracle_sql="SELECT COUNT(*) FROM telco_bss.subscribers s WHERE s.status = 'active' "
        f"AND s.subscriber_id NOT IN (SELECT subscriber_id FROM {INVOICES} "
        "WHERE billing_month = '2026-10-01')",
    )


def _month_slice(ctx: Ctx) -> str:
    """The October invoice slice that carries no condition but the month."""
    for held in ctx.app.catalog.slices_of(INVOICES):
        if len(held.spec.predicates) == 1 and held.complete:
            return held.table_name
    return INVOICES


@scenario(
    "T04",
    sector="telecom",
    title="Congestion events: zoned timestamps and single-precision KPIs",
    sources=("telco_network",),
    capabilities=("timestamptz", "float4", "percentile", "predicate-forms", "time-zone"),
)
def congestion(ctx: Ctx) -> None:
    """Network events carry a timestamp with time zone and a float4 KPI. A condition
    is sent to the source only when both sides are sure to read it the same way;
    whatever stays behind is applied locally, and the answer may not change."""
    events = "telco_network.network_events"

    def report(where: str) -> str:
        return (
            "SELECT w.technology, COUNT(*) AS events, AVG(e.kpi_value) AS avg_kpi, "
            "quantile_cont(e.kpi_value, 0.95) AS p95, MAX(e.severity) AS worst "
            f"FROM {events} e JOIN telco_network.cell_towers w ON w.tower_id = e.tower_id "
            f"WHERE {where} GROUP BY w.technology ORDER BY w.technology"
        )

    with_offset = (
        "e.event_ts >= '2026-10-20 00:00:00+00:00' "
        "AND e.event_ts < '2026-10-27 00:00:00+00:00' AND e.event_type = 'congestion'"
    )
    ctx.sql(
        report(with_offset),
        "a week of congestion, timestamps with an offset",
        ordered=True,
        tolerance=1e-6,
        actions={events: "load_slice"},
    )
    pushed = ctx.pushed(report(with_offset.replace("congestion", "alarm")), events)
    ctx.check(
        "the zoned range reaches the source when the literal names its offset",
        ("event_ts", "gte") in pushed["predicates"] and ("event_ts", "lt") in pushed["predicates"],
        str(pushed),
    )
    bare = (
        "e.event_ts >= '2026-10-20' AND e.event_ts < '2026-10-27' AND e.event_type = 'maintenance'"
    )
    kept = ctx.pushed(report(bare), events)
    ctx.check(
        "without an offset the range stays local",
        not any(column == "event_ts" for column, _ in kept["predicates"]),
        str(kept),
    )
    ctx.sql(
        report(bare),
        "the same week without an offset: same answer, more rows moved",
        ordered=True,
        tolerance=1e-6,
    )
    istanbul = (
        "e.event_ts >= '2026-10-20 00:00:00+03:00' "
        "AND e.event_ts < '2026-10-21 00:00:00+03:00' AND e.event_type = 'congestion'"
    )
    ctx.sql(report(istanbul), "one Istanbul calendar day, by offset", ordered=True, tolerance=1e-6)
    high = with_offset + " AND e.kpi_value > 80.5"
    kept = ctx.pushed(report(high), events)
    ctx.check(
        "a float4 comparison is never sent to the source",
        not any(column == "kpi_value" for column, _ in kept["predicates"]),
        str(kept),
    )
    ctx.sql(report(high), "KPI above 80.5 on a float4 column", ordered=True, tolerance=1e-6)
    ctx.sql(
        "SELECT date_trunc('hour', e.event_ts) AS hour, COUNT(*) AS events "
        f"FROM {events} e WHERE {with_offset} GROUP BY 1 ORDER BY events DESC, hour LIMIT 10",
        "busiest hours (UTC)",
        ordered=True,
    )
    ctx.sql(
        "SELECT hour(e.event_ts AT TIME ZONE 'Europe/Istanbul') AS local_hour, COUNT(*) AS events "
        f"FROM {events} e WHERE {with_offset} GROUP BY 1 ORDER BY 1",
        "events by Istanbul local hour (AT TIME ZONE on a zoned column)",
        ordered=True,
    )


@scenario(
    "T05",
    sector="telecom",
    title="Sixteen analysts asking at once",
    sources=("telco_network", "telco_bss"),
    capabilities=("concurrency", "load-lock", "slice"),
)
def concurrent_analysts(ctx: Ctx) -> None:
    """Sixteen threads fire overlapping questions: several want the same day of call
    records at the same moment. Each slice must be fetched once, and every answer
    must be right."""
    days = [(date(2026, 10, 20) + timedelta(days=n)).isoformat() for n in range(6)]
    work: list[tuple[str, str]] = []
    for round_ in range(4):
        for day in days:
            work.append((f"traffic {day} #{round_}", _day_query(day)))
        work.append(
            (
                f"towers #{round_}",
                "SELECT technology, COUNT(*) AS n FROM "
                "telco_network.cell_towers GROUP BY technology ORDER BY technology",
            )
        )
        work.append(
            (
                f"plans #{round_}",
                "SELECT p.plan_type, COUNT(*) AS n FROM telco_bss.subscribers s "
                "JOIN telco_bss.plans p ON p.plan_id = s.plan_id "
                "GROUP BY p.plan_type ORDER BY p.plan_type",
            )
        )

    def ask(item: tuple[str, str]) -> None:
        ctx.sql(item[1], item[0], ordered=True, tolerance=0.0, use_cache=False)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(ask, work))
    source = ctx.source("telco_network")
    if source is not None:
        fetched = [line for line in source.stats.log if line.startswith("slice cdrs")]
        ctx.check(
            "each day of call records was fetched exactly once",
            len(fetched) == len(days),
            f"{len(fetched)} fetches for {len(days)} days",
        )
    ctx.check(
        "one slice per day is held",
        len(ctx.app.catalog.slices_of(CDRS)) == len(days),
        f"{len(ctx.app.catalog.slices_of(CDRS))} slices",
    )


@scenario(
    "T06",
    sector="telecom",
    title="Six months of billing as a metric series, with the limits raised",
    sources=("telco_bss",),
    capabilities=("semantic", "time-shift", "budget", "volume"),
    budgets=Budgets(full=600_000, slice=4_000_000, total=8_000_000),
)
def billing_series(ctx: Ctx) -> None:
    """A month-over-month billing series needs every invoice of the period: there is
    no filter to shrink it. With the default limits it is refused; with the slice
    limit raised, three million rows of three columns are moved and aggregated."""
    ctx.tool_rows(
        "query_metrics",
        {
            "table": INVOICES,
            "metrics": [
                "amount_due:sum",
                "time_shift(amount_due:sum, periods=1)",
                "change_pct(amount_due:sum)",
                "amount_paid:sum",
                "*:count",
            ],
            "dimensions": ["billing_month:month"],
            "filters": [{"column": "status", "op": "in", "value": ["paid", "partial"]}],
        },
        f"""SELECT m, due, lag(due) OVER (ORDER BY m),
                   (due - lag(due) OVER (ORDER BY m)) / NULLIF(lag(due) OVER (ORDER BY m), 0),
                   paid, n
            FROM (SELECT date_trunc('month', billing_month) AS m, SUM(amount_due) AS due,
                         SUM(amount_paid) AS paid, COUNT(*) AS n
                  FROM {INVOICES} WHERE status IN ('paid', 'partial') GROUP BY 1)
            ORDER BY m""",
        "billed and collected per month, with the previous month beside it",
        ordered=True,
        tolerance=1e-9,
    )
    ctx.check(
        "only the columns the metrics read were moved",
        all(
            s.spec.columns is not None and len(s.spec.columns) <= 4
            for s in ctx.app.catalog.slices_of(INVOICES)
        ),
        str([s.spec.describe() for s in ctx.app.catalog.slices_of(INVOICES)]),
    )
    ctx.tool_rows(
        "query_metrics",
        {
            "table": INVOICES,
            "metrics": ["overage_fee:avg", "overage_fee:max", "*:count"],
            "dimensions": ["status"],
            "filters": [
                {"column": "billing_month", "value": "2026-10-01"},
                {"column": "overage_fee", "op": "gt", "value": 0},
            ],
        },
        f"""SELECT status, AVG(overage_fee), MAX(overage_fee), COUNT(*) FROM {INVOICES}
            WHERE billing_month = '2026-10-01' AND overage_fee > 0 GROUP BY 1 ORDER BY 1""",
        "overage fees in October by payment status",
        ordered=True,
        tolerance=1e-9,
    )
