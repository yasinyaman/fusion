"""Retail: a POS database that answers SQL and an e-commerce database that does not."""

from __future__ import annotations

from scenarios.harness import Budgets, Ctx, scenario

SALES = "retail_pos.sales"
ORDERS = "retail_ecom.web_orders"


@scenario(
    "R01",
    sector="retail",
    title="Store KPIs answered by the POS database itself",
    sources=("retail_pos",),
    capabilities=("pushdown", "cte", "window", "limit"),
)
def store_kpis(ctx: Ctx) -> None:
    """The POS database accepts SQL, so a question that stays inside it is sent there
    whole: nothing is loaded, even for a window over millions of receipt lines."""
    revenue = ctx.sql(
        "SELECT s.region, COUNT(*) AS lines, SUM(x.quantity * x.unit_price) AS revenue "
        f"FROM {SALES} x JOIN retail_pos.stores s ON s.store_id = x.store_id "
        "WHERE x.sale_date >= '2026-10-01' GROUP BY s.region ORDER BY revenue DESC",
        "October revenue by region",
        ordered=True,
        pushdown=True,
        max_moved=0,
    )
    ctx.sql(
        f"""WITH weekly AS (
                SELECT store_id, date_trunc('week', sale_date) AS week,
                       SUM(quantity * unit_price) AS revenue
                FROM {SALES} WHERE sale_date >= '2026-08-01' GROUP BY 1, 2
            ), growth AS (
                SELECT store_id, week, revenue,
                       revenue - lag(revenue) OVER (PARTITION BY store_id ORDER BY week) AS delta
                FROM weekly
            )
            SELECT store_id, week, revenue, delta FROM growth
            WHERE delta IS NOT NULL ORDER BY delta DESC, store_id, week LIMIT 25""",
        "week-over-week growth, a window over the whole table",
        ordered=True,
        pushdown=True,
        max_moved=0,
    )
    ctx.sql(
        f"""SELECT a.product_id AS first, b.product_id AS second, COUNT(*) AS together
            FROM {SALES} a JOIN {SALES} b
              ON a.receipt_id = b.receipt_id AND a.product_id < b.product_id
            WHERE a.sale_date >= '2026-10-24' AND b.sale_date >= '2026-10-24'
            GROUP BY 1, 2 ORDER BY together DESC, first, second LIMIT 20""",
        "market basket: products bought together last week (a self-join)",
        ordered=True,
        pushdown=True,
        max_moved=0,
    )
    ctx.check("the store is still empty", ctx.held() == 0, f"{ctx.held():,} rows held")
    if not ctx.inproc:
        kinds = {type(cell).__name__ for cell in (revenue.rows[0] if revenue else ())}
        ctx.note(
            "a pushed-down answer is JSON from the source",
            f"cell types seen: {sorted(kinds)}; Warp's /query/execute sends no column types, "
            "so a DECIMAL or timestamp arrives as text",
        )
    ctx.fails(
        f"SELECT COUNT(*) FROM {SALES} WHERE store_id = ?",
        "a bound parameter cannot be pushed, and the table cannot be loaded",
        contains="Refusing to load",
        params=[1],
    )
    ctx.sql(
        "SELECT format, COUNT(*) AS n, AVG(sqm) AS avg_sqm FROM retail_pos.stores "
        "WHERE sqm > ? GROUP BY format ORDER BY format",
        "a bound parameter on a small table loads it and runs locally",
        ordered=True,
        params=[500],
        pushdown=False,
    )


@scenario(
    "R02",
    sector="retail",
    title="Promotion uplift with the POS database's SQL switched off",
    sources=("retail_pos",),
    no_pushdown=("retail_pos",),
    capabilities=("slice", "non-equi-join", "two-slices"),
)
def promotion_uplift(ctx: Ctx) -> None:
    """The same POS data behind a gateway with raw SQL disabled, as most production
    gateways are. Now every answer is assembled from slices: receipt lines in the
    promotion window joined to promotions on a date range."""
    uplift = (
        "SELECT p.promo_type, p.discount_pct, COUNT(*) AS lines, SUM(x.quantity) AS units, "
        "SUM(x.quantity * x.unit_price) AS revenue "
        f"FROM {SALES} x JOIN retail_pos.promotions p ON p.product_id = x.product_id "
        "AND x.sale_date >= p.start_date AND x.sale_date <= p.end_date "
        "WHERE x.sale_date >= '2026-09-15' AND p.start_date >= '2026-09-01' "
        "GROUP BY p.promo_type, p.discount_pct ORDER BY p.promo_type, p.discount_pct"
    )
    ctx.sql(
        uplift,
        "units sold under each promotion since mid-September",
        ordered=True,
        pushdown=False,
        actions={SALES: "load_slice", "retail_pos.promotions": "load_full"},
        max_moved=ctx.rows(SALES) // 8 + ctx.rows("retail_pos.promotions"),
    )
    ctx.sql(
        "SELECT s.region, pr.category, COUNT(*) AS empty_shelves "
        "FROM retail_pos.inventory i JOIN retail_pos.stores s ON s.store_id = i.store_id "
        "JOIN retail_pos.products pr ON pr.product_id = i.product_id "
        "WHERE i.on_hand = 0 GROUP BY s.region, pr.category ORDER BY empty_shelves DESC, 1, 2",
        "empty shelves by region and category",
        ordered=True,
        actions={"retail_pos.inventory": "load_slice"},
        max_moved=ctx.rows("retail_pos.inventory") // 8
        + ctx.rows("retail_pos.stores")
        + ctx.rows("retail_pos.products"),
    )
    ctx.sql(
        "SELECT i.store_id, COUNT(DISTINCT i.product_id) AS products, SUM(x.quantity) AS "
        f"units_wanted FROM retail_pos.inventory i JOIN {SALES} x "
        "ON x.store_id = i.store_id AND x.product_id = i.product_id "
        "WHERE i.on_hand = 0 AND x.sale_date >= '2026-10-18' "
        "GROUP BY i.store_id ORDER BY units_wanted DESC, i.store_id LIMIT 30",
        "lost sales: out-of-stock items that sold in the last two weeks (two slices)",
        ordered=True,
        actions={"retail_pos.inventory": ("reuse", "load_slice"), SALES: ("reuse", "load_slice")},
    )
    ctx.limit(
        f"""SELECT a.product_id AS first, b.product_id AS second, COUNT(*) AS together
            FROM {SALES} a JOIN {SALES} b
              ON a.receipt_id = b.receipt_id AND a.product_id < b.product_id
            WHERE a.sale_date >= '2026-10-24' AND b.sale_date >= '2026-10-24'
            GROUP BY 1, 2 ORDER BY together DESC, first, second LIMIT 20""",
        "the market-basket self-join without pushdown (one table under two aliases)",
        ordered=True,
    )
    week = "sale_date >= '2026-10-24'"
    loaded = ctx.tool(
        "load_table",
        {"table": SALES, "where": week, "columns": ["receipt_id", "product_id"]},
        "load last week's receipt lines explicitly",
    )
    table = loaded.get("slice_table", SALES)

    def basket(name: str, extra: str = "") -> str:
        return f"""SELECT a.product_id AS first, b.product_id AS second, COUNT(*) AS together
                   FROM {name} a JOIN {name} b
                     ON a.receipt_id = b.receipt_id AND a.product_id < b.product_id {extra}
                   GROUP BY 1, 2 ORDER BY together DESC, first, second LIMIT 20"""

    ctx.sql(
        basket(table),
        "the self-join on the explicit slice",
        ordered=True,
        max_moved=0,
        oracle_sql=basket(SALES, f"WHERE a.{week} AND b.{week}"),
    )


@scenario(
    "R03",
    sector="retail",
    title="Omnichannel: stores and web shop in one answer",
    sources=("retail_pos", "retail_ecom"),
    capabilities=("federation", "timestamptz", "slice-table", "union"),
    # Returns hold about 60k order keys at full scale; let them all be passed.
    budgets=Budgets(keys=100_000),
)
def omnichannel(ctx: Ctx) -> None:
    """Category revenue across both channels for October. The two fact tables sit in
    different databases and are both too big to load, so each contributes a slice."""
    ctx.sql(
        "SELECT p.category, COUNT(*) AS orders, SUM(o.amount) AS revenue "
        f"FROM {ORDERS} o JOIN retail_pos.products p ON p.product_id = o.product_id "
        "WHERE o.ordered_at >= '2026-10-01 00:00:00+00:00' AND o.status != 'cancelled' "
        "GROUP BY p.category ORDER BY revenue DESC",
        "web revenue by category (orders from one database, products from the other)",
        ordered=True,
        pushdown=False,
        actions={ORDERS: "load_slice"},
        max_moved=ctx.rows(ORDERS) // 8 + ctx.rows("retail_pos.products"),
    )
    union = (
        f"SELECT 'store' AS channel, product_id, quantity * unit_price AS revenue FROM {SALES} "
        "WHERE sale_date >= '2026-10-01' UNION ALL "
        f"SELECT 'web', product_id, amount FROM {ORDERS} "
        "WHERE ordered_at >= '2026-10-01 00:00:00+00:00'"
    )
    ctx.limit(
        f"SELECT channel, SUM(revenue) AS revenue FROM ({union}) GROUP BY channel",
        "a UNION ALL over both fact tables (not a flat SELECT)",
    )
    web = ctx.tool(
        "load_table",
        {
            "table": ORDERS,
            "where": "ordered_at >= '2026-10-01 00:00:00+00:00'",
            "columns": ["product_id", "amount"],
        },
        "web orders slice",
    )
    store = ctx.tool(
        "load_table",
        {
            "table": SALES,
            "where": "sale_date >= '2026-10-01'",
            "columns": ["product_id", "quantity", "unit_price"],
        },
        "store lines slice",
    )
    web_table, store_table = web.get("slice_table", ORDERS), store.get("slice_table", SALES)

    def both(store_from: str, web_from: str) -> str:
        return f"""
            SELECT p.category, c.channel, SUM(c.revenue) AS revenue FROM (
                SELECT 'store' AS channel, product_id, quantity * unit_price AS revenue
                FROM {store_from}
                UNION ALL SELECT 'web', product_id, amount FROM {web_from}
            ) c JOIN retail_pos.products p ON p.product_id = c.product_id
            GROUP BY p.category, c.channel ORDER BY p.category, c.channel"""

    ctx.sql(
        both(store_table, web_table),
        "both channels by category, over the two slices",
        ordered=True,
        max_moved=0,
        oracle_sql=both(
            f"{SALES} WHERE sale_date >= '2026-10-01'",
            f"{ORDERS} WHERE ordered_at >= '2026-10-01 00:00:00+00:00'",
        ),
    )
    ctx.sql(
        "SELECT r.reason, COUNT(*) AS returns, SUM(r.refunded_amount) AS refunded, "
        "AVG(o.amount) AS avg_order "
        f"FROM retail_ecom.returns r JOIN {ORDERS} o ON o.order_id = r.order_id "
        "GROUP BY r.reason ORDER BY refunded DESC",
        "returns by reason: the orders are fetched by the keys returns hold",
        ordered=True,
        actions={ORDERS: "semi_join"},
        max_moved=ctx.rows("retail_ecom.returns") * 2 + 10,
    )


@scenario(
    "R04",
    sector="retail",
    title="An assistant explores the catalogue with the tools alone",
    sources=("retail_pos", "retail_ecom"),
    capabilities=("tools", "search", "aggregate", "unicode", "injection"),
)
def assistant(ctx: Ctx) -> None:
    """What an LLM does with no SQL of its own: describe, search, aggregate. Turkish
    text has to match exactly, and hostile arguments have to be turned away."""
    described = ctx.tool("describe_table", {"table": "retail_pos.products"}, "describe products")
    ctx.check(
        "types come back as the source declares them",
        any(
            c["name"] == "list_price" and "decimal" in c["type"]
            for c in described.get("columns", [])
        ),
    )
    found = ctx.tool(
        "search_data",
        {
            "table": "retail_pos.products",
            "filter_column": "product_name",
            "filter_value": "Çikolata%",
            "limit": 20,
        },
        "search products LIKE 'Çikolata%'",
    )
    total = ctx.scalar(
        "SELECT count(*) FROM retail_pos.products WHERE product_name LIKE 'Çikolata%'"
    )
    rows = found.get("rows", [])
    ctx.check(
        "every hit starts with Çikolata and the count is right",
        len(rows) == min(20, total) and all(r["product_name"].startswith("Çikolata") for r in rows),
        f"{len(rows)} rows, {total} at the source",
    )
    exact = ctx.tool(
        "search_data",
        {
            "table": "retail_pos.stores",
            "filter_column": "city",
            "filter_value": "Iğdır",
            "limit": 100,
        },
        "dotless-i city matches exactly",
    )
    cities = {r["city"] for r in exact.get("rows", [])}
    ctx.check("Iğdır is not confused with any other spelling", cities <= {"Iğdır"}, str(cities))
    ctx.tool_rows(
        "aggregate_data",
        {"table": SALES, "group_by": "payment_method", "agg_column": "quantity", "agg_func": "SUM"},
        f"SELECT payment_method, SUM(quantity) AS s FROM {SALES} GROUP BY 1 ORDER BY s DESC",
        "units by payment method (pushed to the POS database)",
        ordered=True,
    )
    ctx.tool(
        "aggregate_data",
        {"table": ORDERS, "group_by": "status", "agg_column": "amount", "agg_func": "SUM"},
        "the same on the web shop needs the whole table and is refused",
        expect_error="Refusing to load",
    )
    hits = ctx.tool(
        "search_data",
        {
            "table": "retail_ecom.web_sessions",
            "filter_column": "device",
            "filter_value": "tablet",
            "limit": 10,
        },
        "search a table too big to load",
    )
    ctx.check(
        "only tablet sessions come back",
        len(hits.get("rows", [])) == 10
        and all(r["device"] == "tablet" for r in hits.get("rows", [])),
    )
    ctx.check(
        "the search fetched the matching rows, not the table",
        not ctx.inproc or ctx.moved() < ctx.rows("retail_ecom.web_sessions") // 4,
        f"{ctx.moved():,} rows moved",
    )
    ctx.tool(
        "aggregate_data",
        {
            "table": SALES,
            "group_by": "payment_method",
            "agg_column": "quantity); DROP TABLE x;--",
            "agg_func": "SUM",
        },
        "SQL in a column name is rejected",
        expect_error="Invalid agg_column",
    )
    ctx.tool(
        "aggregate_data",
        {
            "table": SALES,
            "group_by": "payment_method",
            "agg_column": "quantity",
            "agg_func": "SUM); DROP TABLE x",
        },
        "SQL in the function name is rejected",
        expect_error="Invalid aggregation function",
    )
    ctx.tool(
        "aggregate_data",
        {
            "table": SALES,
            "group_by": "payment_method",
            "agg_column": "no_such_column",
            "agg_func": "SUM",
        },
        "an unknown column is named in the error",
        expect_error="no_such_column",
    )
    injected = ctx.tool(
        "search_data",
        {"table": "retail_pos.products", "filter_column": "brand", "filter_value": "' OR 1=1 --"},
        "a quote in the search value is data, not SQL",
    )
    ctx.check("the injection matched nothing", injected.get("row_count") == 0)
    ctx.tool(
        "describe_table",
        {"table": "retail_pos.prodcuts"},
        "a misspelt table gets a useful error",
        expect_error="prod",
    )


@scenario(
    "R05",
    sector="retail",
    title="Price and ranking metrics from the semantic layer",
    sources=("retail_pos",),
    capabilities=("semantic", "weighted-avg", "rank", "ntile", "never-pushed"),
)
def price_metrics(ctx: Ctx) -> None:
    """Average selling price weighted by units, and stores ranked into quartiles.
    Metric queries are compiled for the local store and never pushed down, so even
    the SQL-capable POS database serves them as slices."""
    ctx.tool_rows(
        "query_metrics",
        {
            "table": SALES,
            "metrics": [
                "unit_price:weighted_avg(weight=quantity)",
                "quantity:sum",
                "receipt_id:count_distinct",
            ],
            "dimensions": ["sale_date:week"],
            "filters": [{"column": "sale_date", "op": "gte", "value": "2026-09-01"}],
        },
        f"""SELECT date_trunc('week', sale_date) AS w,
                   SUM(unit_price * quantity) / NULLIF(SUM(quantity), 0), SUM(quantity),
                   COUNT(DISTINCT receipt_id)
            FROM {SALES} WHERE sale_date >= '2026-09-01' GROUP BY 1 ORDER BY 1""",
        "weekly average selling price, weighted by units",
        ordered=True,
        tolerance=1e-9,
    )
    ctx.check(
        "the metric was computed from a slice, not pushed down",
        not ctx.inproc
        or (
            ctx.source("retail_pos").stats.pushdowns == 0  # type: ignore[union-attr]
            and ctx.held() > 0
        ),
    )
    ctx.tool_rows(
        "query_metrics",
        {
            "table": SALES,
            "metrics": [
                "quantity:sum",
                "ntile(quantity:sum, buckets=4)",
                "rank(quantity:sum)",
                "percent_rank(quantity:sum)",
            ],
            "dimensions": ["store_id"],
            "filters": [{"column": "sale_date", "op": "gte", "value": "2026-10-01"}],
            "order_by": "-quantity_sum",
            "limit": 100,
        },
        f"""SELECT store_id, s, NTILE(4) OVER (ORDER BY s DESC), RANK() OVER (ORDER BY s DESC),
                   PERCENT_RANK() OVER (ORDER BY s DESC)
            FROM (SELECT store_id, SUM(quantity) AS s FROM {SALES}
                  WHERE sale_date >= '2026-10-01' GROUP BY 1)
            ORDER BY s DESC LIMIT 100""",
        "stores ranked and split into quartiles",
        tolerance=1e-9,
    )
    ctx.tool(
        "query_metrics",
        {"table": SALES, "metrics": ["unit_price:weighted_avg"], "dimensions": ["sale_date:month"]},
        "a weighted average without a weight explains what is missing",
        expect_error="weight",
    )
    ctx.tool(
        "query_metrics",
        {"table": SALES, "metrics": ["time_shift(quantity:sum)"], "dimensions": ["store_id"]},
        "a calendar transform without a time dimension is refused",
        expect_error="time dimension",
    )
