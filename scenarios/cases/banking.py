"""Banking: core banking behind a gateway that refuses raw SQL, cards on one that allows it."""

from __future__ import annotations

from scenarios.harness import Budgets, Ctx, scenario

TXN = "bank_core.transactions"


@scenario(
    "B01",
    sector="banking",
    title="Regional volume for one month, from a table too big to load",
    sources=("bank_core",),
    capabilities=("refusal", "slice", "slice-reuse", "join"),
)
def regional_month(ctx: Ctx) -> None:
    """A regional manager asks for September's posted volume by region. The movements
    table is far over the load limit, so the bare table is refused with advice, the
    month is fetched as a slice, and a narrower follow-up reads the slice already held."""
    ctx.refused(
        f"SELECT * FROM {TXN}",
        "the whole movements table is refused, with advice",
        contains=("Refusing to load", "full_load_max_rows", "load_table("),
    )

    def by_region(start: str, end: str) -> str:
        return (
            "SELECT b.region, COUNT(*) AS movements, SUM(t.amount) AS volume "
            f"FROM {TXN} t "
            "JOIN bank_core.accounts a ON a.account_id = t.account_id "
            "JOIN bank_core.customers c ON c.customer_id = a.customer_id "
            "JOIN bank_core.branches b ON b.branch_id = c.branch_id "
            f"WHERE t.txn_date >= '{start}' AND t.txn_date < '{end}' AND t.status = 'posted' "
            "GROUP BY b.region ORDER BY volume DESC"
        )

    month = by_region("2026-09-01", "2026-10-01")
    ctx.sql(
        month,
        "September by region",
        ordered=True,
        actions={TXN: "load_slice"},
        max_moved=ctx.rows(TXN) // 10
        + ctx.rows("bank_core.accounts")
        + ctx.rows("bank_core.customers")
        + ctx.rows("bank_core.branches"),
    )
    ctx.sql(
        by_region("2026-09-10", "2026-09-20"),
        "ten days inside it reuse the slice",
        ordered=True,
        actions={TXN: "reuse"},
        max_moved=0,
    )
    ctx.sql(
        month,
        "the same question again comes from the cache",
        ordered=True,
        from_cache=True,
        max_moved=0,
    )
    ctx.sql(
        by_region("2026-08-01", "2026-10-01"),
        "a wider range is fetched, not guessed",
        ordered=True,
        actions={TXN: "load_slice"},
    )
    eighteen_months = by_region("2025-01-01", "2026-07-01")
    ctx.refused(
        eighteen_months,
        "eighteen months is over the slice limit and says so",
        contains=("narrow it further",),
    )


@scenario(
    "B02",
    sector="banking",
    title="AML structuring: deposits kept just under the reporting threshold",
    sources=("bank_core",),
    capabilities=("refusal", "load_table", "slice-table", "window", "cte"),
)
def structuring(ctx: Ctx) -> None:
    """Compliance looks for accounts making repeated cash deposits just below 50,000 TL.
    A window over the raw table is refused, so the analyst loads the suspicious sliver
    explicitly and runs the rolling-window analysis on the slice."""
    window = (
        "SELECT account_id, txn_date, SUM(amount) OVER (PARTITION BY account_id "
        f"ORDER BY txn_date) AS running FROM {TXN} WHERE txn_type = 'cash_deposit'"
    )
    ctx.refused(window, "a window over the raw table is refused")

    where = "txn_type = 'cash_deposit' AND amount >= 45000 AND amount < 50000"
    loaded = ctx.tool(
        "load_table",
        {"table": TXN, "where": where, "columns": ["account_id", "txn_date", "amount"]},
        "load only the near-threshold deposits",
    )
    slice_table = loaded.get("slice_table", TXN)
    expected = ctx.scalar(f"SELECT count(*) FROM {TXN} WHERE {where}")
    ctx.check(
        "the slice holds exactly the near-threshold deposits",
        loaded.get("row_count") == expected and loaded.get("complete") is True,
        f"slice {loaded.get('row_count')} rows vs {expected} at the source",
    )

    def analysis(table: str, extra: str = "") -> str:
        return f"""
            WITH daily AS (
                SELECT account_id, txn_date, COUNT(*) AS deposits, SUM(amount) AS total
                FROM {table} {extra} GROUP BY account_id, txn_date
            ), rolling AS (
                SELECT account_id, txn_date,
                       SUM(deposits) OVER w AS deposits_90d, SUM(total) OVER w AS total_90d
                FROM daily
                WINDOW w AS (PARTITION BY account_id ORDER BY txn_date
                             RANGE BETWEEN INTERVAL 90 DAY PRECEDING AND CURRENT ROW)
            )
            SELECT account_id, MAX(deposits_90d) AS worst_90d, MAX(total_90d) AS worst_total
            FROM rolling GROUP BY account_id HAVING MAX(deposits_90d) >= 2
            ORDER BY worst_total DESC, account_id
        """

    result = ctx.sql(
        analysis(slice_table),
        "rolling 90-day structuring on the slice",
        ordered=True,
        oracle_sql=analysis(TXN, f"WHERE {where}"),
        max_moved=0,
    )
    if result is not None:
        seeded = [row[0] for row in result.rows if row[0] % 499 == 0]
        ctx.check(
            "the seeded structuring accounts are what it finds",
            bool(result.rows) and len(seeded) >= 0.9 * len(result.rows),
            f"{len(seeded)} of {len(result.rows)} flagged accounts carry the pattern",
        )
    ctx.sql(
        f"SELECT a.account_type, COUNT(*) AS deposits, SUM(s.amount) AS total "
        f"FROM {slice_table} s JOIN bank_core.accounts a ON a.account_id = s.account_id "
        "GROUP BY a.account_type ORDER BY total DESC",
        "the slice joins to a catalog table",
        ordered=True,
        oracle_sql=f"SELECT a.account_type, COUNT(*), SUM(s.amount) AS total FROM {TXN} s "
        f"JOIN bank_core.accounts a ON a.account_id = s.account_id WHERE "
        + where.replace("txn_type", "s.txn_type").replace("amount", "s.amount")
        + " GROUP BY a.account_type ORDER BY total DESC",
    )


@scenario(
    "B03",
    sector="banking",
    title="Watchlist screening: fetch a huge table by the keys a small one holds",
    sources=("bank_core",),
    capabilities=("semi-join", "driver-filter", "limit"),
)
def watchlist(ctx: Ctx) -> None:
    """Accounts under review are a few thousand; their movements are a sliver of
    millions. Joining the two fetches the movements by key instead of refusing."""

    def screening(extra: str = "") -> str:
        return (
            "SELECT w.reason, COUNT(DISTINCT t.account_id) AS accounts, COUNT(*) AS movements, "
            f"SUM(t.amount) AS volume FROM bank_core.watchlist w JOIN {TXN} t "
            f"ON t.account_id = w.account_id {extra} GROUP BY w.reason ORDER BY w.reason"
        )

    ctx.sql(
        screening("WHERE w.reason = 'PEP'"),
        "movements of politically exposed accounts",
        ordered=True,
        actions={TXN: "semi_join"},
        max_moved=ctx.rows(TXN) // 10,
    )
    ctx.sql(
        screening(),
        "movements of every watched account",
        ordered=True,
        actions={TXN: "semi_join"},
        max_moved=ctx.rows(TXN) // 4,
    )
    ctx.sql(
        screening("WHERE t.txn_date >= '2026-10-01' AND w.reason = 'yaptırım'"),
        "with a date on the big side it becomes a slice",
        ordered=True,
    )
    ctx.limit(
        f"SELECT COUNT(*) AS n, SUM(t.amount) AS volume FROM bank_core.accounts a JOIN {TXN} t "
        "ON t.account_id = a.account_id WHERE a.customer_id = 42",
        "one customer's movements through accounts (the driver is planned at its full size)",
    )
    ctx.sql(
        f"SELECT COUNT(*) AS n, SUM(amount) AS volume FROM {TXN} "
        "WHERE account_id IN (499, 998, 1497)",
        "the same intent with the keys spelled out",
    )


@scenario(
    "B04",
    sector="banking",
    title="Card fraud triage across the card platform and core banking",
    sources=("bank_core", "bank_cards"),
    capabilities=("federation", "semi-join", "pushdown", "timestamptz", "float4"),
)
def fraud_triage(ctx: Ctx) -> None:
    """Open fraud alerts live in the card platform, the customer's segment in core
    banking. The join crosses two databases; a question that stays inside the card
    platform is answered by the platform itself."""
    cards = "bank_cards.card_transactions"
    ctx.sql(
        f"SELECT response_code, COUNT(*) AS n, SUM(amount) AS total FROM {cards} "
        "GROUP BY response_code ORDER BY response_code",
        "approval mix: the card platform answers it itself",
        ordered=True,
        pushdown=True,
        max_moved=0,
    )
    ctx.check(
        "nothing was loaded for a pushed-down answer", ctx.held() == 0, f"{ctx.held():,} rows held"
    )
    ctx.sql(
        "SELECT c.segment, f.severity, COUNT(*) AS alerts, SUM(ct.amount) AS exposure, "
        "AVG(ct.fraud_score) AS avg_score "
        f"FROM bank_cards.fraud_alerts f JOIN {cards} ct ON ct.ctx_id = f.ctx_id "
        "JOIN bank_cards.cards k ON k.card_id = ct.card_id "
        "JOIN bank_core.customers c ON c.customer_id = k.customer_id "
        "WHERE f.resolved = FALSE GROUP BY c.segment, f.severity ORDER BY c.segment, f.severity",
        "open alerts by customer segment (two databases)",
        ordered=True,
        pushdown=False,
        actions={cards: "semi_join"},
        max_moved=ctx.rows(cards) // 10
        + ctx.rows("bank_cards.fraud_alerts")
        + ctx.rows("bank_cards.cards")
        + ctx.rows("bank_core.customers"),
        tolerance=1e-6,
    )
    ctx.sql(
        "SELECT card_type, status, COUNT(*) AS n FROM bank_cards.cards "
        "GROUP BY card_type, status ORDER BY card_type, status",
        "a table already loaded is answered locally",
        ordered=True,
        pushdown=False,
        max_moved=0,
    )
    ctx.sql(
        f"SELECT is_ecommerce, COUNT(*) AS n, AVG(fraud_score) AS score FROM {cards} "
        "WHERE auth_ts >= '2026-10-01 00:00:00+03:00' GROUP BY is_ecommerce ORDER BY 1",
        "October in Istanbul time: still the platform's own answer",
        ordered=True,
        pushdown=True,
        tolerance=1e-6,
    )
    ctx.sql(
        "SELECT m.category, COUNT(*) AS n, SUM(ct.amount) AS total, c.segment "
        f"FROM {cards} ct JOIN bank_cards.merchants m ON m.merchant_id = ct.merchant_id "
        "JOIN bank_cards.cards k ON k.card_id = ct.card_id "
        "JOIN bank_core.customers c ON c.customer_id = k.customer_id "
        "WHERE ct.auth_ts >= '2026-10-25 00:00:00+00:00' AND ct.fraud_score > 0.5 "
        "AND c.segment = 'private' GROUP BY m.category, c.segment ORDER BY total DESC, 1",
        "high-score private-banking spend: zoned filter pushed, float filter kept local",
        ordered=True,
        actions={cards: ("load_slice", "reuse")},
    )


@scenario(
    "B05",
    sector="banking",
    title="Monthly trend from the semantic layer, no SQL written",
    sources=("bank_core",),
    capabilities=("semantic", "transforms", "slice"),
)
def metric_trend(ctx: Ctx) -> None:
    """An assistant answers 'how is mobile volume trending' with metric expressions.
    Only the columns and rows the metrics need are fetched from the movements table."""
    model = ctx.tool("list_metrics", {"table": TXN}, "what can be measured on movements")
    ctx.check(
        "the model offers amount and a time dimension",
        any(m["name"] == "amount" for m in model.get("measures", []))
        and any(d["name"] == "txn_date" for d in model.get("dimensions", [])),
    )
    filters = [
        {"column": "txn_date", "op": "gte", "value": "2026-07-01"},
        {"column": "channel", "value": "mobile"},
    ]
    ctx.tool_rows(
        "query_metrics",
        {
            "table": TXN,
            "metrics": ["amount:sum", "*:count", "change_pct(amount:sum)", "cumsum(amount:sum)"],
            "dimensions": ["txn_date:month"],
            "filters": filters,
        },
        f"""SELECT m, s, n, (s - lag(s) OVER (ORDER BY m)) / NULLIF(lag(s) OVER (ORDER BY m), 0),
                   SUM(s) OVER (ORDER BY m)
            FROM (SELECT date_trunc('month', txn_date) AS m, SUM(amount) AS s, COUNT(*) AS n
                  FROM {TXN} WHERE txn_date >= '2026-07-01' AND channel = 'mobile' GROUP BY 1)
            ORDER BY m""",
        "mobile volume by month with change and running total",
        ordered=True,
        tolerance=1e-9,
    )
    ctx.check(
        "only the needed columns crossed the wire",
        all(
            s.spec.columns is not None and len(s.spec.columns) <= 3
            for s in ctx.app.catalog.all_slices()
        ),
        str([s.spec.describe() for s in ctx.app.catalog.all_slices()]),
    )
    ctx.tool_rows(
        "query_metrics",
        {
            "table": TXN,
            "metrics": ["amount:sum", "rank(amount:sum)", "amount:median"],
            "dimensions": ["channel"],
            "filters": [{"column": "txn_date", "op": "gte", "value": "2026-10-01"}],
            "order_by": "-amount_sum",
        },
        f"""SELECT channel, SUM(amount) AS s, RANK() OVER (ORDER BY SUM(amount) DESC),
                   MEDIAN(amount) FROM {TXN} WHERE txn_date >= '2026-10-01'
            GROUP BY channel ORDER BY s DESC""",
        "channels ranked by October volume",
        ordered=True,
        tolerance=1e-9,
    )
    ctx.tool(
        "query_metrics",
        {"table": TXN, "metrics": ["amount:sum"], "dimensions": ["channel"]},
        "without a filter the table is still too big, and it says so",
        expect_error="Refusing to load",
    )
    ctx.tool(
        "query_metrics",
        {"table": TXN, "metrics": ["amount:total"], "dimensions": ["channel"]},
        "an unknown aggregation is explained, not guessed",
        expect_error="Unknown aggregation",
    )


@scenario(
    "B06",
    sector="banking",
    title="Non-performing loans as a materialized view on a schedule",
    sources=("bank_core",),
    capabilities=("materialized-view", "scheduler", "refusal"),
)
def npl_view(ctx: Ctx) -> None:
    """Risk wants the NPL ratio by region and product as a standing table, refreshed
    on a schedule. A view over the movements table is refused: views read whole tables."""
    definition = (
        "SELECT b.region, l.product, COUNT(*) AS loans, SUM(l.principal) AS exposure, "
        "SUM(l.principal) FILTER (WHERE l.days_past_due >= 90) AS npl_exposure, "
        "COALESCE(SUM(l.principal) FILTER (WHERE l.days_past_due >= 90), 0) / SUM(l.principal) "
        "AS npl_ratio FROM bank_core.loans l "
        "JOIN bank_core.customers c ON c.customer_id = l.customer_id "
        "JOIN bank_core.branches b ON b.branch_id = c.branch_id "
        "WHERE l.status != 'closed' GROUP BY b.region, l.product"
    )
    ctx.tool(
        "create_view",
        {"name": "npl_by_region", "sql": definition, "refresh": "every 15 minutes"},
        "create the view",
    )
    ctx.sql(
        "SELECT * FROM mv_npl_by_region ORDER BY region, product",
        "the view matches the source",
        ordered=True,
        oracle_sql=f"SELECT * FROM ({definition}) ORDER BY region, product",
        tolerance=1e-9,
    )
    ctx.sql(
        "SELECT region, SUM(npl_exposure) / SUM(exposure) AS ratio FROM mv_npl_by_region "
        "GROUP BY region ORDER BY ratio DESC NULLS LAST, region",
        "a roll-up over the view",
        ordered=True,
        oracle_sql=f"SELECT region, SUM(npl_exposure) / SUM(exposure) AS ratio FROM "
        f"({definition}) GROUP BY region ORDER BY ratio DESC NULLS LAST, region",
    )
    views = ctx.tool("list_views", {}, "the view is listed")
    ctx.check("list_views reports it", "npl_by_region" in str(views))
    before = ctx.moved()
    ctx.clock.advance(16 * 60)  # a refresh skips tables read more recently than its interval
    ran = ctx.scheduler.tick()
    ctx.check(
        "the schedule fired and re-read the tables it depends on",
        ran >= 1 and (not ctx.inproc or ctx.moved() > before),
        f"{ran} jobs ran, {ctx.moved() - before:,} rows re-read",
    )
    ctx.tool("refresh_view", {"name": "npl_by_region"}, "a manual refresh works too")
    ctx.tool(
        "create_view",
        {"name": "volume", "sql": f"SELECT channel, SUM(amount) AS v FROM {TXN} GROUP BY channel"},
        "a view over the movements table is refused",
        expect_error="materialized view loads",
    )
    ctx.tool(
        "create_view",
        {"name": "fast", "sql": "SELECT 1 AS x", "refresh": "every 5 seconds"},
        "a refresh faster than the minimum is refused",
        expect_error="too short",
    )
    ctx.tool(
        "create_view",
        {"name": "odd", "sql": "SELECT 1 AS x", "refresh": "whenever"},
        "an unknown interval is refused",
        expect_error="Unknown refresh interval",
    )


@scenario(
    "B07",
    sector="banking",
    title="End-of-day reconciliation, to the kuruş",
    sources=("bank_core",),
    capabilities=("decimal", "slice", "predicate-forms", "limit"),
    budgets=Budgets(total=1_200_000),
)
def reconciliation(ctx: Ctx) -> None:
    """Finance reconciles a day's movements by currency and channel. Decimal totals
    must match the source exactly, and the same day asked in different ways of
    writing the filter must give the same figures."""

    def totals(where: str) -> str:
        return (
            f"SELECT currency, channel, COUNT(*) AS n, SUM(amount) AS total, "
            f"MIN(amount) AS smallest, MAX(amount) AS largest FROM {TXN} WHERE {where} "
            "GROUP BY currency, channel ORDER BY currency, channel"
        )

    day = ctx.sql(
        totals("txn_date = '2026-10-15'"),
        "one day by currency and channel",
        ordered=True,
        tolerance=0.0,
        actions={TXN: "load_slice"},
    )
    ctx.sql(
        totals("txn_date >= '2026-10-15' AND txn_date <= '2026-10-15'"),
        "the day as a closed range",
        ordered=True,
        tolerance=0.0,
    )
    ctx.sql(
        totals("txn_date IN ('2026-10-15')"), "the day as an IN list", ordered=True, tolerance=0.0
    )
    ctx.sql(
        totals("txn_date = '2026-10-15' AND currency = 'USD' AND amount > 1000"),
        "a narrower question is answered from the day already held",
        ordered=True,
        tolerance=0.0,
        actions={TXN: "reuse"},
        max_moved=0,
    )
    ctx.sql(
        totals("txn_date IN ('2026-10-13', '2026-10-14', '2026-10-15') AND status != 'reversed'"),
        "three days, with a text != that stays local",
        ordered=True,
        tolerance=0.0,
    )
    ctx.sql(
        "SELECT account_id, SUM(CASE WHEN direction = 'credit' THEN amount ELSE -amount END) "
        f"AS net FROM {TXN} WHERE txn_date = '2026-10-15' GROUP BY account_id "
        "ORDER BY net DESC, account_id LIMIT 50",
        "net position per account needs other columns, so it is fetched",
        ordered=True,
        tolerance=0.0,
        actions={TXN: "load_slice"},
        max_moved=ctx.rows(TXN) // 100,
    )
    if day is not None:
        ctx.check("the day has movements in more than one currency", len(day.rows) > 3)
    ctx.limit(
        totals("txn_date BETWEEN '2026-10-15' AND '2026-10-15'"),
        "BETWEEN on a date (not read as a range)",
        ordered=True,
    )
    ctx.limit(totals("txn_date = DATE '2026-10-15'"), "a typed DATE literal", ordered=True)
    ctx.limit(
        totals("txn_date = '2026-10-15' OR txn_date = '2026-10-16'"),
        "two days joined by OR",
        ordered=True,
    )
    ctx.limit(totals("txn_date = CURRENT_DATE - 30"), "a date computed by a function", ordered=True)
