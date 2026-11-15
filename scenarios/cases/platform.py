"""Platform: guardrails, cache, limits, the tool contract and the REST API.

Sector-neutral, run over the banking data because it has every column type.
"""

from __future__ import annotations

from scenarios.harness import Ctx, scenario

TXN = "bank_core.transactions"

HOSTILE = [
    ("DROP TABLE bank_core.customers", "DROP"),
    ("DELETE FROM bank_core.customers", "DELETE"),
    ("INSERT INTO bank_core.branches VALUES (1)", "INSERT"),
    ("UPDATE bank_core.accounts SET balance = 0", "UPDATE"),
    ("CREATE TABLE x AS SELECT 1", "CREATE"),
    ("SELECT 1; DROP TABLE bank_core.customers", "two statements"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
    ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "read_parquet over the network"),
    ("SELECT * FROM '/etc/passwd'", "a file path as a table"),
    ("SELECT * FROM 'https://example.com/x.csv'", "a URL as a table"),
    ("COPY bank_core.customers TO '/tmp/out.csv'", "COPY out"),
    ("ATTACH '/tmp/other.duckdb' AS o", "ATTACH"),
    ("INSTALL httpfs", "INSTALL"),
    ("LOAD httpfs", "LOAD"),
    ("SET enable_external_access = true", "SET"),
    ("PRAGMA database_list", "PRAGMA"),
    ("EXPORT DATABASE '/tmp/dump'", "EXPORT DATABASE"),
    ("CALL pragma_version()", "CALL"),
    ("SELECT * FROM glob('/etc/*')", "glob"),
    ("SELECT read_text('/etc/hosts')", "read_text"),
    ("SELECT 1 /* harmless */ ; /* */ DELETE FROM bank_core.loans", "comment-wrapped second"),
    ("WITH x AS (DELETE FROM bank_core.loans RETURNING *) SELECT * FROM x", "DML in a CTE"),
]


@scenario(
    "P01",
    sector="platform",
    title="Guardrails against hostile SQL",
    sources=("bank_core",),
    capabilities=("guardrails", "security"),
)
def guardrails(ctx: Ctx) -> None:
    """Everything an LLM could be talked into sending: writes, second statements, file
    and network reads, settings. Each must stop before the store, and nothing may be
    fetched from the source on the way."""
    for sql, what in HOSTILE:
        ctx.blocked(sql, f"blocked: {what}")
    result = ctx.tool(
        "query_data",
        {"sql": "DROP TABLE bank_core.customers"},
        "through the tool the block is an error result",
        expect_error="",
    )
    ctx.check("the tool returned an error dict, not an exception", "error" in result)
    ctx.tool(
        "create_view",
        {"name": "evil", "sql": "SELECT * FROM read_csv('/etc/passwd')"},
        "a view cannot smuggle a file read",
        expect_error="",
    )
    ctx.tool(
        "create_view",
        {"name": "x; DROP", "sql": "SELECT 1"},
        "a view name with SQL in it is refused",
        expect_error="Invalid view name",
    )
    ctx.sql(
        "SELECT 'DROP TABLE x; read_csv(''/etc/passwd'')' AS text_only",
        "the same words inside a string literal are just text",
    )


@scenario(
    "P02",
    sector="platform",
    title="Cache: what is shared, what is not, and what clears it",
    sources=("bank_core",),
    capabilities=("cache", "params", "tool-contract"),
)
def cache(ctx: Ctx) -> None:
    """The cache keys on normalized SQL plus bound parameters. Formatting changes hit;
    a different parameter misses; a changed answer is never served."""
    base = "SELECT segment, COUNT(*) AS n FROM bank_core.customers GROUP BY segment ORDER BY 1"
    ctx.sql(base, "first run", ordered=True, from_cache=False)
    ctx.sql(
        base.replace(" ", "   ").replace("GROUP BY", "\n GROUP BY"),
        "the same query, reformatted, is a hit",
        ordered=True,
        from_cache=True,
    )
    ctx.sql(base.replace("SELECT", "SELECT /* report */"), "with a comment", ordered=True)
    q = "SELECT COUNT(*) AS n FROM bank_core.customers WHERE risk_score > ?"
    ctx.sql(q, "parameter 50", params=[50], from_cache=False)
    ctx.sql(q, "parameter 90 is a different answer", params=[90], from_cache=False)
    ctx.sql(q, "parameter 50 again is a hit", params=[50], from_cache=True)
    stats = ctx.tool("cache_stats", {}, "cache_stats")
    ctx.check("cache_stats reports hits", "hit" in str(stats).lower(), str(stats)[:200])
    big = ctx.tool(
        "query_data",
        {"sql": "SELECT * FROM bank_core.customers ORDER BY 1"},
        "query_data on a result larger than 100 rows",
    )
    ctx.check(
        "query_data caps at 100 rows and says it was cut",
        big.get("row_count") == 100 and big.get("truncated") is True,
        f"row_count={big.get('row_count')} truncated={big.get('truncated')}",
    )
    twin = ctx.tool(
        "query_data",
        {
            "sql": "SELECT c.customer_id, a.customer_id FROM "
            "bank_core.customers c JOIN bank_core.accounts a "
            "ON a.customer_id = c.customer_id LIMIT 3"
        },
        "two columns with one name",
    )
    ctx.check(
        "repeated names come back as customer_id, customer_id_2",
        twin.get("columns") == ["customer_id", "customer_id_2"],
        str(twin.get("columns")),
    )
    ctx.tool("no_such_tool", {}, "an unknown tool is an error", expect_error="")
    ctx.tool(
        "load_table",
        {"table": "bank_core.customers; DROP"},
        "a table name with SQL in it is refused",
        expect_error="",
    )


@scenario(
    "P03",
    sector="platform",
    title="A runaway query is stopped and the engine keeps working",
    sources=("bank_core",),
    capabilities=("timeout", "recovery"),
    settings={"query_timeout": 1.0},
)
def runaway(ctx: Ctx) -> None:
    """A three-way cross join over customers would run for hours. The store's watchdog
    has to interrupt it at the timeout, and the next query must run normally."""
    ctx.sql("SELECT COUNT(*) FROM bank_core.customers", "load customers")
    ctx.fails(
        "SELECT COUNT(*) FROM bank_core.customers a, bank_core.customers b, "
        "bank_core.customers c, bank_core.customers d WHERE a.risk_score + b.risk_score "
        "+ c.risk_score + d.risk_score = 123",
        "the cross join is interrupted",
        contains="",
    )
    ctx.sql(
        "SELECT segment, COUNT(*) AS n FROM bank_core.customers GROUP BY 1 ORDER BY 1",
        "the next query runs",
        ordered=True,
    )


@scenario(
    "P04",
    sector="platform",
    title="The REST API in front of the same engine",
    sources=("bank_core",),
    capabilities=("rest", "auth", "tools"),
    settings={"api_key": "scenario-key-0123456789", "rate_limit": "1000/minute"},
)
def rest(ctx: Ctx) -> None:
    """What an HTTP client sees: health, readiness, API-key auth, a query, a tool call
    and a refusal that keeps its advice."""
    try:
        from fastapi.testclient import TestClient

        from fusion.adapters.inbound.rest.app import create_app
    except ImportError as e:  # the rest extra is not installed
        ctx.note("REST extra not installed", str(e))
        return
    client = TestClient(create_app(ctx.app))
    key = {"X-API-Key": "scenario-key-0123456789"}
    health = client.get("/health")
    ctx.check("/health answers without a key", health.status_code == 200, health.text[:200])
    ctx.check(
        "/readiness sees the source", client.get("/readiness", headers=key).status_code == 200
    )
    denied = client.post("/query", json={"sql": "SELECT 1"})
    ctx.check("/query without a key is 401", denied.status_code == 401, str(denied.status_code))
    wrong = client.post("/query", json={"sql": "SELECT 1"}, headers={"X-API-Key": "nope"})
    ctx.check(
        "/query with a wrong key is refused",
        wrong.status_code in (401, 403),
        str(wrong.status_code),
    )
    sql = "SELECT segment, COUNT(*) AS n FROM bank_core.customers GROUP BY 1 ORDER BY 1"
    answered = client.post("/query", json={"sql": sql}, headers=key)
    body = answered.json() if answered.status_code == 200 else {}
    want = ctx.oracle(sql)
    got = [tuple(r.values()) for r in body.get("rows", [])]
    ctx.check(
        "/query returns the right rows",
        got == [tuple(r) for r in want],
        f"{answered.status_code}: {answered.text[:200]}",
    )
    tool = client.post("/tools/describe_table", json={"table": TXN}, headers=key)
    ctx.check(
        "/tools/describe_table works",
        tool.status_code == 200 and "amount" in tool.text,
        tool.text[:200],
    )
    refused = client.post("/query", json={"sql": f"SELECT * FROM {TXN}"}, headers=key)
    ctx.check(
        "a refusal comes back as a client error with the advice in it",
        400 <= refused.status_code < 500 and "load_table" in refused.text,
        f"{refused.status_code}: {refused.text[:200]}",
    )
    blocked = client.post("/query", json={"sql": "DROP TABLE bank_core.customers"}, headers=key)
    ctx.check(
        "a blocked statement is a client error",
        400 <= blocked.status_code < 500,
        f"{blocked.status_code}: {blocked.text[:200]}",
    )
    huge = client.post(
        "/query",
        content=b'{"sql": "' + b"x" * 2_000_000 + b'"}',
        headers={**key, "Content-Type": "application/json"},
    )
    ctx.check(
        "a 2 MB body is refused before it is parsed", huge.status_code == 413, str(huge.status_code)
    )
