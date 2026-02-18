"""Fusion v5 demo — Smart Data Fetching + Multi-Source Federation.

Demonstrates:
- Metadata-only connection (no eager data loading)
- FetchStrategy: SQL table extraction via sqlglot
- On-demand lazy table loading
- load_table tool for explicit loading
- Cross-source federation (JOINs across different Warp sources)
- ToolExecutor with 10 tools (MCP + OpenAI)
- SQL guardrails via tools
- Cache performance
"""

import json
import random
import string
import time

import numpy as np
import pandas as pd

from fusion import OLAPEngine, FetchStrategy
from fusion.tools import ToolExecutor, get_openai_tools, get_mcp_tools


def generate_synthetic_data():
    """Generate synthetic e-commerce data (885K rows)."""
    segments = ["premium", "standard", "basic", "enterprise", "trial"]
    categories = ["Electronics", "Clothing", "Food", "Books", "Sports", "Home", "Toys", "Beauty"]

    users = pd.DataFrame({
        "id": range(1, 5001),
        "name": [f"User_{i}" for i in range(1, 5001)],
        "email": [f"user{i}@example.com" for i in range(1, 5001)],
        "segment": [random.choice(segments) for _ in range(5000)],
        "created_at": pd.date_range("2023-01-01", periods=5000, freq="h"),
    })

    products = pd.DataFrame({
        "id": range(1, 201),
        "name": [f"Product_{i}" for i in range(1, 201)],
        "category": [random.choice(categories) for _ in range(200)],
        "price": np.round(np.random.uniform(5, 500, 200), 2),
    })

    product_ids = np.random.choice(products["id"].values, 100_000)
    price_map = dict(zip(products["id"].values, products["price"].values))

    orders = pd.DataFrame({
        "id": range(1, 100_001),
        "user_id": np.random.choice(users["id"].values, 100_000),
        "product_id": product_ids,
        "amount": np.array([price_map[pid] * np.random.uniform(0.8, 1.2) for pid in product_ids]).round(2),
        "order_date": pd.date_range("2024-01-01", periods=100_000, freq="5min"),
    })

    sessions = pd.DataFrame({
        "session_id": [f"sess_{''.join(random.choices(string.hexdigits[:16], k=12))}" for _ in range(200_000)],
        "user_id": np.random.choice(users["id"].values, 200_000),
        "duration_sec": np.random.exponential(300, 200_000).astype(int),
        "page_views": np.random.poisson(5, 200_000),
        "created_at": pd.date_range("2024-01-01", periods=200_000, freq="2min"),
    })

    events = pd.DataFrame({
        "event_id": range(1, 500_001),
        "session_id": np.random.choice(sessions["session_id"].values, 500_000),
        "event_type": [random.choice(["page_view", "click", "scroll", "form_submit", "purchase", "search", "logout"]) for _ in range(500_000)],
        "timestamp": pd.date_range("2024-01-01", periods=500_000, freq="1min"),
    })

    transactions = pd.DataFrame({
        "tx_id": range(1, 80_001),
        "user_id": np.random.choice(users["id"].values, 80_000),
        "amount": np.round(np.random.uniform(10, 1000, 80_000), 2),
        "year": np.random.choice([2022, 2023, 2024], 80_000, p=[0.2, 0.3, 0.5]),
        "tx_date": pd.date_range("2022-01-01", periods=80_000, freq="15min"),
    })

    return users, products, orders, sessions, events, transactions


def register_metadata_only(engine, users, products, orders, sessions, events, transactions):
    """Register metadata WITHOUT loading data — simulates metadata-only connect_source()."""
    def cols_from_df(df):
        return [{"name": c, "type": str(df[c].dtype), "nullable": True} for c in df.columns]

    # Create schemas
    engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_ecommerce")
    engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_analytics")
    engine.execute_raw("CREATE SCHEMA IF NOT EXISTS warp_finance")

    # Register METADATA ONLY — no data loaded!
    engine.catalog.register_source("warp_ecommerce", "warp", {
        "users": {"columns": cols_from_df(users), "row_count": len(users)},
        "products": {"columns": cols_from_df(products), "row_count": len(products)},
        "orders": {"columns": cols_from_df(orders), "row_count": len(orders)},
    })
    engine.catalog.register_source("warp_analytics", "warp", {
        "sessions": {"columns": cols_from_df(sessions), "row_count": len(sessions)},
        "events": {"columns": cols_from_df(events), "row_count": len(events)},
    })
    engine.catalog.register_source("warp_finance", "warp", {
        "transactions": {"columns": cols_from_df(transactions), "row_count": len(transactions)},
    })

    # Store DataFrames for on-demand loading simulation
    return {
        "warp_ecommerce.users": users,
        "warp_ecommerce.products": products,
        "warp_ecommerce.orders": orders,
        "warp_analytics.sessions": sessions,
        "warp_analytics.events": events,
        "warp_finance.transactions": transactions,
    }


def simulate_on_demand_load(engine, data_store, tables):
    """Simulate on-demand loading by materializing requested tables."""
    loaded = []
    for full_name in tables:
        if engine.catalog.is_loaded(full_name):
            continue
        if full_name in data_store:
            df = data_store[full_name]
            with engine._lock:
                engine._conn.execute(
                    f"CREATE OR REPLACE TABLE {full_name} AS SELECT * FROM df"
                )
            engine.catalog.mark_loaded(full_name)
            loaded.append(full_name)
    return loaded


def run_demo():
    """Run Fusion v3 demo."""
    print("=" * 70)
    print("  Fusion v5 — Smart Data Fetching + Federation Demo")
    print("  Lazy Loading + Cross-Source JOINs + 10 Tools")
    print("=" * 70)

    # --- Step 1: Generate data ---
    print("\n[1/9] Generating synthetic data (885K rows)...")
    t0 = time.perf_counter()
    users, products, orders, sessions, events, transactions = generate_synthetic_data()
    total = sum(len(df) for df in [users, products, orders, sessions, events, transactions])
    print(f"  Generated {total:,} rows in {(time.perf_counter()-t0)*1000:.0f}ms")

    # --- Step 2: Metadata-only connection ---
    print("\n[2/9] Connecting sources (METADATA ONLY — no data loaded!)...")
    t0 = time.perf_counter()
    engine = OLAPEngine(database=":memory:", threads=4, memory_limit="4GB")
    data_store = register_metadata_only(engine, users, products, orders, sessions, events, transactions)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  Connected in {elapsed:.0f}ms (metadata only)")

    # Show that NO tables are loaded
    all_tables = engine.catalog.list_tables()
    unloaded = engine.catalog.list_unloaded_tables()
    print(f"  Tables in catalog: {len(all_tables)}")
    print(f"  Tables loaded in DuckDB: {len(all_tables) - len(unloaded)} (none!)")
    print(f"  Tables NOT loaded: {len(unloaded)}")
    for t in unloaded:
        print(f"    - {t} (loaded={engine.catalog.is_loaded(t)})")

    # --- Step 3: FetchStrategy demo ---
    print("\n[3/9] FetchStrategy — SQL table extraction...")
    strategy = FetchStrategy(engine.catalog)

    test_queries = [
        "SELECT * FROM warp_ecommerce.orders WHERE amount > 100",
        "SELECT o.*, u.name FROM warp_ecommerce.orders o JOIN warp_ecommerce.users u ON o.user_id = u.id",
        "SELECT o.*, e.event_type FROM warp_ecommerce.orders o JOIN warp_analytics.events e ON o.id = e.event_id",
    ]

    for sql in test_queries:
        plan = strategy.plan_for_sql(sql)
        targets = [t.full_name for t in plan.targets]
        print(f"  SQL: {sql[:70]}...")
        print(f"    -> Tables needed: {targets}")

    # --- Step 4: On-demand loading ---
    print("\n[4/9] On-demand loading — only fetch what's needed...")

    # Simulate: user asks about orders+users -> only load those 2 tables
    plan = strategy.plan_for_sql("""
        SELECT u.segment, COUNT(*) as orders, ROUND(SUM(o.amount), 2) as revenue
        FROM warp_ecommerce.orders o
        JOIN warp_ecommerce.users u ON o.user_id = u.id
        GROUP BY u.segment ORDER BY revenue DESC
    """)
    needed = [t.full_name for t in plan.targets]
    print(f"  Query needs: {needed}")

    t0 = time.perf_counter()
    loaded = simulate_on_demand_load(engine, data_store, needed)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  Loaded {len(loaded)} tables in {elapsed:.0f}ms: {loaded}")
    print(f"  Tables still unloaded: {engine.catalog.list_unloaded_tables()}")

    # Now execute the query
    result = engine.sql("""
        SELECT u.segment, COUNT(*) as orders, ROUND(SUM(o.amount), 2) as revenue
        FROM warp_ecommerce.orders o
        JOIN warp_ecommerce.users u ON o.user_id = u.id
        GROUP BY u.segment ORDER BY revenue DESC
    """, auto_load=False)
    print(f"\n  Query result: {result.row_count} rows, {result.execution_time:.1f}ms")
    for row in result.to_dict():
        print(f"    {row['segment']:12s} | orders={row['orders']:,} | revenue=${row['revenue']:,.2f}")

    # --- Step 5: ToolExecutor with lazy loading ---
    print("\n[5/9] ToolExecutor — 10 tools...")
    executor = ToolExecutor(engine)

    # list_sources shows loaded status
    result = executor.execute("list_sources", {})
    print("\n  [tool: list_sources]")
    for src in result["sources"]:
        for t in src["tables"]:
            status = "LOADED" if t["loaded"] else "NOT LOADED"
            print(f"    {t['name']:40s} rows={t['row_count']:>8,}  [{status}]")

    # load_table tool
    print("\n  [tool: load_table] — Explicit table loading")
    simulate_on_demand_load(engine, data_store, ["warp_ecommerce.products"])
    result = executor.execute("load_table", {"table": "warp_ecommerce.products"})
    print(f"    load_table('warp_ecommerce.products') -> {result}")

    # --- Step 6: Cross-source federation ---
    print("\n[6/9] Cross-source federation — JOIN across Warp sources...")
    # Load analytics tables
    simulate_on_demand_load(engine, data_store, [
        "warp_analytics.sessions",
        "warp_analytics.events",
    ])

    result = executor.execute("query_data", {"sql": """
        SELECT u.segment,
               COUNT(DISTINCT s.session_id) as sessions,
               ROUND(AVG(s.duration_sec), 0) as avg_duration
        FROM warp_ecommerce.users u
        JOIN warp_analytics.sessions s ON u.id = s.user_id
        GROUP BY u.segment ORDER BY sessions DESC
    """})
    print("  Cross-source JOIN: warp_ecommerce.users + warp_analytics.sessions")
    print(f"  Result: {result['row_count']} rows")
    for row in result["rows"]:
        print(f"    {row['segment']:12s} | sessions={row['sessions']:,} | avg_duration={row['avg_duration']}s")

    # --- Step 7: Tool format demos ---
    print("\n[7/9] Tool format demos...")
    openai_tools = get_openai_tools()
    mcp_tools = get_mcp_tools()
    print(f"  OpenAI tools: {len(openai_tools)} definitions")
    print(f"  MCP tools:    {len(mcp_tools)} definitions")
    tool_names = [t["function"]["name"] for t in openai_tools]
    print(f"  Tool names: {tool_names}")

    # --- Step 8: Cache performance ---
    print("\n[8/9] Cache performance...")
    test_sql = "SELECT segment, COUNT(*) as cnt FROM warp_ecommerce.users GROUP BY segment"
    r1 = engine.sql(test_sql, auto_load=False)
    r2 = engine.sql(test_sql, auto_load=False)
    print(f"  First call:  {r1.execution_time:.1f}ms (cached={r1.from_cache})")
    print(f"  Second call: {r2.execution_time:.1f}ms (cached={r2.from_cache})")

    # --- Step 9: SQL Guardrails ---
    print("\n[9/9] SQL guardrails through tool layer...")
    dangerous_queries = [
        "DROP TABLE warp_ecommerce.users",
        "DELETE FROM warp_ecommerce.orders WHERE 1=1",
        "INSERT INTO warp_ecommerce.users VALUES (9999, 'hack')",
    ]
    for sql in dangerous_queries:
        result = executor.execute("query_data", {"sql": sql})
        status = "BLOCKED" if "error" in result else "PASSED (unexpected!)"
        print(f"  [{status}] {sql[:60]}")

    safe = executor.execute("query_data", {"sql": "SELECT COUNT(*) as cnt FROM warp_ecommerce.users"})
    print(f"  [SAFE] SELECT COUNT(*) -> {safe['rows'][0]['cnt']:,} rows")

    # --- Cleanup ---
    engine.close()

    print("\n" + "=" * 70)
    print("  Fusion v5 demo completed successfully!")
    print("  Key features demonstrated:")
    print("    - Metadata-only connection (no eager data loading)")
    print("    - FetchStrategy: SQL table extraction via sqlglot")
    print("    - On-demand lazy table loading")
    print("    - Cross-source federation (JOINs across Warp sources)")
    print("    - 10 tools (MCP + OpenAI)")
    print("=" * 70)


if __name__ == "__main__":
    run_demo()
