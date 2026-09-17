"""Fusion demo — lazy loading, cross-source federation and the 10 LLM tools.

Runs entirely in-process: synthetic pandas DataFrames stand in for three
Warp databases through ``FrameSource``, a tiny DataSource adapter. Everything
else is the real thing: FusionApp, the planner, lazy loading into DuckDB
(under the external-access security latch), guardrails, cache and tools.

    python -m demo.demo            # full size (~885K rows)
    python -m demo.demo --scale 0.1
"""

from __future__ import annotations

import argparse
import json
import random
import string
import time
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from fusion import Settings, build_app, get_mcp_tools, get_openai_tools
from fusion.adapters.inbound.sdk.formats import rowset_from_dataframe
from fusion.domain.models import (
    ColumnInfo,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
    TableSchema,
)
from fusion.domain.slices import SliceSpec
from fusion.ports.data_source import fetch_slice_in_memory

# ---------------------------------------------------------------------------
# FrameSource: a DataSource backed by in-memory DataFrames
# ---------------------------------------------------------------------------


class FrameSource:
    """Serves pandas DataFrames through the DataSource port (no pushdown)."""

    source_type = "frames"

    def __init__(self, name: str, frames: Mapping[str, pd.DataFrame]) -> None:
        self.name = name
        self._frames = dict(frames)
        self.fetches: list[str] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def discover_schema(self) -> SourceSchema:
        schema: SourceSchema = {}
        for table, df in self._frames.items():
            columns = [
                ColumnInfo(str(col), _sql_type(str(df[col].dtype)), bool(df[col].isna().any()))
                for col in df.columns
            ]
            schema[table] = TableSchema(columns=columns, row_count=len(df))
        return schema

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        self.fetches.append(table)
        df = self._frames[table]
        if max_rows is not None:
            df = df.head(max_rows)
        return rowset_from_dataframe(df)

    def fetch_slice(
        self, table: str, spec: SliceSpec = SliceSpec.FULL, max_rows: int | None = None
    ) -> RowStream:
        """Frames cannot filter themselves, so the slicing happens here."""
        return fetch_slice_in_memory(self, table, spec, max_rows)

    def estimate_slice(self, table: str, spec: SliceSpec = SliceSpec.FULL) -> int | None:
        df = self._frames.get(table)
        if df is None:
            return None
        if spec.is_full or not spec.predicates:
            return len(df)
        return sum(1 for record in df.to_dict("records") if spec.matches(record))

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(pushdown=False, slices=True, arrow=False, row_estimates=True)

    @property
    def supports_pushdown(self) -> bool:
        return False


def _sql_type(dtype: str) -> str:
    dtype = dtype.lower()
    if "int" in dtype:
        return "integer"
    if "float" in dtype:
        return "double"
    if "bool" in dtype:
        return "boolean"
    if "datetime" in dtype:
        return "timestamp"
    return "varchar"


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------


def generate_synthetic_data(
    scale: float = 1.0, seed: int = 42
) -> dict[str, dict[str, pd.DataFrame]]:
    """Three "databases" of e-commerce data; ``scale`` shrinks every table."""
    rng = np.random.default_rng(seed)
    random.seed(seed)
    n_users = max(10, int(5_000 * scale))
    n_products = max(5, int(200 * scale))
    n_orders = max(20, int(100_000 * scale))
    n_sessions = max(20, int(200_000 * scale))
    n_events = max(20, int(500_000 * scale))
    n_tx = max(20, int(80_000 * scale))

    segments = ["premium", "standard", "basic", "enterprise", "trial"]
    categories = ["Electronics", "Clothing", "Food", "Books", "Sports", "Home", "Toys", "Beauty"]

    users = pd.DataFrame(
        {
            "id": range(1, n_users + 1),
            "name": [f"User_{i}" for i in range(1, n_users + 1)],
            "email": [f"user{i}@example.com" for i in range(1, n_users + 1)],
            "segment": rng.choice(segments, n_users),
            "created_at": pd.date_range("2023-01-01", periods=n_users, freq="h"),
        }
    )
    products = pd.DataFrame(
        {
            "id": range(1, n_products + 1),
            "name": [f"Product_{i}" for i in range(1, n_products + 1)],
            "category": rng.choice(categories, n_products),
            "price": np.round(rng.uniform(5, 500, n_products), 2),
        }
    )
    product_ids = rng.choice(products["id"].to_numpy(), n_orders)
    price_map = dict(zip(products["id"].tolist(), products["price"].tolist(), strict=True))
    orders = pd.DataFrame(
        {
            "id": range(1, n_orders + 1),
            "user_id": rng.choice(users["id"].to_numpy(), n_orders),
            "product_id": product_ids,
            "amount": np.round(
                np.array([price_map[int(pid)] for pid in product_ids])
                * rng.uniform(0.8, 1.2, n_orders),
                2,
            ),
            "order_date": pd.date_range("2024-01-01", periods=n_orders, freq="5min"),
        }
    )
    sessions = pd.DataFrame(
        {
            "session_id": [
                "sess_" + "".join(random.choices(string.hexdigits[:16], k=12))
                for _ in range(n_sessions)
            ],
            "user_id": rng.choice(users["id"].to_numpy(), n_sessions),
            "duration_sec": rng.exponential(300, n_sessions).astype(int),
            "device": rng.choice(["mobile", "desktop", "tablet"], n_sessions),
        }
    )
    events = pd.DataFrame(
        {
            "event_id": range(1, n_events + 1),
            "user_id": rng.choice(users["id"].to_numpy(), n_events),
            "event_type": rng.choice(["view", "click", "cart", "purchase"], n_events),
            "ts": pd.date_range("2024-01-01", periods=n_events, freq="min"),
        }
    )
    transactions = pd.DataFrame(
        {
            "tx_id": range(1, n_tx + 1),
            "order_id": rng.choice(orders["id"].to_numpy(), n_tx),
            "method": rng.choice(["card", "bank", "wallet"], n_tx),
            "fee": np.round(rng.uniform(0.1, 5.0, n_tx), 2),
        }
    )
    return {
        "warp_ecommerce": {"users": users, "products": products, "orders": orders},
        "warp_analytics": {"sessions": sessions, "events": events},
        "warp_finance": {"transactions": transactions},
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def _ms(started: float) -> str:
    return f"{(time.perf_counter() - started) * 1000:.0f}ms"


def run_demo(scale: float = 1.0, quiet: bool = False) -> dict[str, Any]:
    """Run every step; returns a few facts so tests can assert on them."""
    say = (lambda *a, **k: None) if quiet else print
    facts: dict[str, Any] = {}

    say("=" * 70)
    say("  Fusion demo — lazy loading, federation, 10 LLM tools")
    say("=" * 70)

    say("\n[1/9] Generating synthetic data...")
    t0 = time.perf_counter()
    frames = generate_synthetic_data(scale)
    total = sum(len(df) for db in frames.values() for df in db.values())
    say(f"  Generated {total:,} rows in {_ms(t0)}")
    facts["rows_generated"] = total

    say("\n[2/9] Building the app and connecting sources (metadata only)...")
    sources: dict[str, FrameSource] = {}

    def factory(name: str, config: Mapping[str, Any]) -> FrameSource:
        sources[name] = FrameSource(name, frames[name])
        return sources[name]

    app = build_app(Settings(memory_limit="4GB", threads=4), source_factory=factory)
    try:
        t0 = time.perf_counter()
        for name in frames:
            app.sources.connect(name, {"type": "frames"})
        say(f"  Connected {len(frames)} sources in {_ms(t0)}")
        tables = app.catalog.list_tables()
        say(f"  Tables in catalog: {len(tables)}")
        say(f"  Loaded in DuckDB : {sum(app.catalog.is_loaded(t) for t in tables)}")
        for ref in tables:
            say(f"    - {ref} (loaded={app.catalog.is_loaded(ref)})")
        facts["tables"] = len(tables)

        say("\n[3/9] FetchPlanner — which tables does a query need?")
        planner = app.views._planner  # the same planner every service uses
        for sql in [
            "SELECT * FROM warp_ecommerce.orders WHERE amount > 100",
            "SELECT o.*, u.name FROM warp_ecommerce.orders o "
            "JOIN warp_ecommerce.users u ON o.user_id = u.id",
            "SELECT o.*, e.event_type FROM warp_ecommerce.orders o "
            "JOIN warp_analytics.events e ON o.user_id = e.user_id",
        ]:
            plan = planner.plan_for_sql(sql)
            say(f"  {sql[:60]}...")
            say(f"    -> needs: {[t.full_name for t in plan.targets]}")

        say("\n[4/9] Lazy loading — the first query pulls only what it references...")
        t0 = time.perf_counter()
        result = app.query.sql(
            "SELECT u.segment, COUNT(*) AS orders, ROUND(SUM(o.amount), 2) AS revenue "
            "FROM warp_ecommerce.orders o JOIN warp_ecommerce.users u ON o.user_id = u.id "
            "GROUP BY u.segment ORDER BY revenue DESC"
        )
        fetched = list(sources["warp_ecommerce"].fetches)
        say(f"  {result.row_count} rows in {_ms(t0)}; loaded: {fetched}")
        say(result.to_markdown())
        facts["loaded_after_first_query"] = fetched
        assert not app.catalog.is_loaded("warp_analytics.events")

        say("\n[5/9] load_table tool — explicit loading via the tool layer...")
        say(f"  {app.tools.execute('load_table', {'table': 'warp_finance.transactions'})}")
        say(f"  {app.tools.execute('load_table', {'table': 'warp_finance.transactions'})}")

        say("\n[6/9] Cross-source federation — JOIN across three 'databases'...")
        t0 = time.perf_counter()
        result = app.query.sql(
            "SELECT t.method, COUNT(*) AS payments, ROUND(SUM(o.amount), 2) AS volume, "
            "COUNT(DISTINCT s.device) AS devices "
            "FROM warp_finance.transactions t "
            "JOIN warp_ecommerce.orders o ON t.order_id = o.id "
            "JOIN warp_analytics.sessions s ON s.user_id = o.user_id "
            "GROUP BY t.method ORDER BY volume DESC"
        )
        say(f"  {result.row_count} rows in {_ms(t0)}")
        say(result.to_markdown())
        facts["federation_rows"] = result.row_count

        say("\n[7/9] Tools — the same 10 tools an LLM calls (MCP / OpenAI)...")
        say(f"  OpenAI tool defs: {len(get_openai_tools())}, MCP tool defs: {len(get_mcp_tools())}")
        agg = app.tools.execute(
            "aggregate_data",
            {
                "table": "warp_analytics.sessions",
                "group_by": "device",
                "agg_column": "duration_sec",
                "agg_func": "AVG",
            },
        )
        say(f"  aggregate_data -> {json.dumps(agg['rows'][:3], default=str)}")
        search = app.tools.execute(
            "search_data",
            {
                "table": "warp_ecommerce.products",
                "filter_column": "category",
                "filter_value": "Books",
            },
        )
        say(f"  search_data    -> {search['row_count']} rows")
        view = app.tools.execute(
            "create_view",
            {
                "name": "revenue_by_category",
                "sql": "SELECT p.category, ROUND(SUM(o.amount), 2) AS revenue "
                "FROM warp_ecommerce.orders o "
                "JOIN warp_ecommerce.products p ON o.product_id = p.id "
                "GROUP BY p.category",
            },
        )
        say(f"  create_view    -> {view}")
        top = app.tools.execute(
            "query_data",
            {"sql": "SELECT * FROM mv_revenue_by_category ORDER BY revenue DESC LIMIT 3"},
        )
        say(f"  query_data(mv) -> {json.dumps(top['rows'], default=str)}")
        facts["view_rows"] = top["row_count"]

        say("\n[8/9] Guardrails — destructive SQL is refused before it reaches DuckDB...")
        for sql in ["DROP TABLE warp_ecommerce.orders", "SELECT * FROM read_csv('/etc/passwd')"]:
            error = app.tools.execute("query_data", {"sql": sql})["error"]
            say(f"  {sql[:45]:45s} -> {error[:60]}")

        say("\n[9/9] Cache — repeat a query...")
        sql = "SELECT COUNT(*) AS n FROM warp_ecommerce.orders"
        t0 = time.perf_counter()
        first = app.query.sql(sql)
        t_first = _ms(t0)
        t0 = time.perf_counter()
        second = app.query.sql(sql)
        say(f"  first : {t_first} (cache={first.from_cache})")
        say(f"  second: {_ms(t0)} (cache={second.from_cache})")
        say(f"  {app.tools.execute('cache_stats', {})}")
        facts["cache_hit"] = second.from_cache
    finally:
        app.close()

    say("\nDone.")
    return facts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fusion demo")
    parser.add_argument("--scale", type=float, default=1.0, help="dataset scale (default 1.0)")
    args = parser.parse_args(argv)
    run_demo(scale=args.scale)


if __name__ == "__main__":
    main()
