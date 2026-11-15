# Sector scenarios

Synthetic source databases for six sectors, and 33 scenarios that push Fusion
through every branch of its planner: whole-table loads, slices, slice reuse,
semi-joins, refusals, pushdown, federation across databases, the semantic layer,
materialized views, incremental refresh, the row budget and eviction,
concurrency, guardrails, timeouts and the REST API.

Every answer is checked against an **oracle** that runs the same SQL on the
source data directly. A scenario passes only when Fusion's rows equal the
oracle's, the planner chose what the scenario expects, and no more rows left the
source than the question needs.

```bash
uv run --frozen python -m scenarios generate --scale 0.1   # ~6.6M rows, a few seconds
uv run --frozen python -m scenarios list -v
uv run --frozen python -m scenarios run --scale 0.1
uv run --frozen python -m scenarios run --scale 1 --sector telecom
uv run --frozen python -m scenarios run --scale 1 --only B03,T05 --latency-ms 20
uv run --frozen pytest scenarios/tests                       # the harness itself
```

Reports land in `scenarios/results/` (Markdown for reading, JSON for diffing
runs); the data in `scenarios/.data/`. Both are git-ignored.

## The data

| Database | Sector | Engine | Raw SQL | Largest table (scale 1) |
|---|---|---|---|---|
| `bank_core` | banking | Postgres | no | `transactions` 5M |
| `bank_cards` | banking | MySQL | yes | `card_transactions` 4M |
| `retail_pos` | retail | Postgres | yes | `sales` 6M |
| `retail_ecom` | retail | MySQL | no | `web_sessions` 3M |
| `telco_bss` | telecom | Postgres | no | `invoices` 3M |
| `telco_network` | telecom | Postgres | no | `cdrs` 10M |
| `hospital` | healthcare | Postgres | no | `lab_results` 4M |
| `factory_mes` | manufacturing | Postgres | no | `quality_checks` 1.2M |
| `factory_iot` | manufacturing | Postgres | no | `sensor_readings` 8M |
| `logistics` | logistics | MySQL | no | `tracking_events` 7M |

66M rows in all at scale 1.0 (about 1 GB of DuckDB files, 45 s to generate).
Every value is a function of the row number (`hash(i, salt)`), so a scale
always produces the same rows. The data is made to be awkward on purpose:
skewed keys, Turkish text (İ/ı/ş/ğ), `DECIMAL`, `REAL`, `TIMESTAMPTZ`, `UUID`,
NULLs in measures and keys, and planted patterns the scenarios must find
(structuring deposits, faulty towers, overheating machines, wide-spread
batches).

The row budgets scale with the data (`Budgets` in `harness.py`), so a table
too big to load at scale 1.0 is too big at 0.01 as well and the planner takes
the same branch.

## The scenarios

| Id | Scenario | What it exercises |
|---|---|---|
| B01 | Regional volume for one month | refusal, slice, slice reuse, cache |
| B02 | AML structuring | `load_table`, window on a slice table |
| B03 | Watchlist screening | semi-join, driver filter |
| B04 | Card fraud triage | federation, pushdown, `TIMESTAMPTZ`, float4 |
| B05 | Monthly trend, no SQL | semantic layer, transforms |
| B06 | NPL as a materialized view | views, scheduler, view refusal |
| B07 | End-of-day reconciliation | exact decimals, predicate forms |
| R01 | Store KPIs on the POS database | pushdown of windows and self-joins |
| R02 | Promotion uplift without raw SQL | non-equi join, two slices |
| R03 | Omnichannel | federation, `UNION ALL` over slices |
| R04 | An assistant with tools only | search, aggregate, Unicode, injection |
| R05 | Price and ranking metrics | weighted average, rank, ntile |
| T01 | Daily reports under a tight budget | eviction, budget refusal |
| T02 | Dropped calls per tower | semi-join, Unicode |
| T03 | Revenue assurance | two slices across databases |
| T04 | Congestion events | zoned timestamps, float4, time zones |
| T05 | Sixteen analysts at once | concurrency, one fetch per slice |
| T06 | Six months of billing | semantic layer at volume |
| H01 | 30-day readmissions | window on a slice, NULLs |
| H02 | Diabetic HbA1c results | two slices, pending (NULL) results |
| H03 | Drug interactions | self-join on a slice |
| M01 | Sensor anomalies | rolling z-score, finds planted faults |
| M02 | Process capability (Cpk) | statistics on a slice |
| M03 | Downtime Pareto | windows over small tables, ASOF join |
| M04 | A live line | incremental refresh, upsert, deletes, cache |
| L01 | SLA by service and origin | NULLs, percentiles, semantic layer |
| L02 | Last scan per parcel | semi-join, `arg_max`, HAVING |
| L03 | Lookups by tracking UUID | point lookups |
| L04 | Gateway outage and capped reads | outage, recovery, partial slices |
| P01 | Hostile SQL | guardrails |
| P02 | Cache | normalisation, parameters, tool contract |
| P03 | A runaway query | timeout, recovery |
| P04 | The REST API | auth, errors, body limit |

A step of kind `limit` records a question the engine refuses by design (a
window or self-join over a table too big to load, `OR`, `BETWEEN`, typed
literals). It passes when Fusion refuses cleanly or answers right; only a wrong
answer or a crash fails it.

## In-process and Warp runs

The default `--mode inproc` serves the DuckDB files through `DuckSource`, a
stand-in for Warp that filters and projects on its side, estimates sizes and
hands rows over as Warp's own Arrow stream. Everything above the data-source
port is the real engine.

`--mode warp` runs the same scenarios against real databases (Postgres 16, MySQL 8 and
two Warp instances, one with raw SQL off and one on):

```bash
uv run --frozen python -m scenarios export --scale 0.1   # writes scenarios/.stack/
cd scenarios/.stack && docker compose up -d --build       # or docker-compose
# wait until both databases accept TCP connections (the healthchecks do), then:
uv run --frozen python -m scenarios run --mode warp --scale 0.1 \
    --warp-url http://localhost:8000 --warp-sql-url http://localhost:8001
docker compose down -v
```

Use scale 0.1 on a laptop: at 1.0 the CSVs are about 4.5 GB and each database wants
several GB of memory to load. Set `DOCKER_CONFIG` to a directory holding `{}` if
`docker-credential-desktop` is missing.

The export maps types per engine; on MySQL a zoned timestamp becomes a UTC
`DATETIME(6)` and a UUID `CHAR(36)`, and text compares under the
accent-insensitive default collation, which is what a real MySQL source does.
M04 and L04 change or break the source and run in-process only.

Through a real Warp, a pushed-down answer comes back as JSON with no column types
(DECIMAL and timestamps as text); the harness compares those as text and records it as a
note in R01.

## Adding a scenario

```python
@scenario("X01", sector="banking", title="...", sources=("bank_core",), capabilities=("slice",))
def my_case(ctx: Ctx) -> None:
    """The story, shown in the report."""
    ctx.sql(
        "SELECT ...",
        "label",
        actions={"bank_core.transactions": "load_slice"},
        max_moved=ctx.rows("bank_core.transactions") // 10,
    )
    ctx.refused("SELECT * FROM bank_core.transactions")
    ctx.tool("load_table", {...})
```

`ctx.sql` compares with the oracle; `actions`, `pushdown`, `from_cache` and
`max_moved` add expectations. `ctx.refused`, `ctx.blocked`, `ctx.fails` and
`ctx.limit` cover the ways a query should not run. `ctx.scratch` creates a
writable source for scenarios that change data.
