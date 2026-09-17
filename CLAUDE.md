# Fusion OLAP Engine

DuckDB-powered in-memory analytics engine — LLM tool for data access via MCP, REST API, and the Python SDK. Hexagonal (ports & adapters) architecture since 1.0.0.

## Project Overview

- **Language**: Python 3.12+ (CI: 3.12 / 3.13 / 3.14)
- **Version**: 1.1.0 (smart transfer: slices, streaming ingest, semi-joins, incremental refresh)
- **Core dependencies**: DuckDB 1.5+, pyarrow, requests, sqlglot 30 (pandas is an optional extra)
- **Data source**: [Warp](https://github.com/yasinyaman/warp) REST API (PostgreSQL/MySQL)
- **Tooling**: uv (`uv.lock` is committed), ruff, mypy 2, pytest 9

## Commands

- Install: `uv sync --all-extras`
- Test: `uv run pytest` (coverage gate `fail_under = 80`)
- Lint: `uv run ruff check fusion tests demo` and `uv run ruff format --check fusion tests demo`
- Types: `uv run mypy fusion` (strict flags on `domain`, `ports`, `application`)
- Lock check: `uv lock --check`
- Demo: `uv run python -m demo.demo --scale 0.1`
- MCP server: `uv run fusion-mcp --warp-url http://localhost:8000 --database mydb`
- REST server: `uv run fusion-rest --warp-url http://localhost:8000 --auto-discover --port 9000`

Always validate with `uv run --frozen ...`; the locked tool versions are newer than typical system installs.

## Architecture

```
inbound adapters (REST, MCP, CLI, SDK)  ──►  application (FusionApp + services)  ──►  ports (Protocols)
                                                       │                                   ▲
                                                       ▼                                   │ implemented by
                                              domain (pure Python)             outbound adapters (DuckDB, sqlglot,
                                                                                memory cache, scheduler, Warp)
fusion.bootstrap.build_app(settings) wires adapters into the services.
```

Dependency rule (enforced by `tests/architecture/test_dependency_rules.py`, which walks every import including lazy ones):

| Layer | May import | Third-party |
|---|---|---|
| `fusion.domain` | domain | no |
| `fusion.ports` | domain, ports | no |
| `fusion.application` | domain, ports, application | no |
| `fusion.observability` | domain, application, observability | no |
| `fusion.adapters.outbound.*` | domain, ports, outbound | duckdb, pyarrow, sqlglot, requests |
| `fusion.adapters.inbound.*` | domain, ports, application, observability, inbound, `fusion.bootstrap`, `fusion` | fastapi, starlette, slowapi, pydantic, uvicorn, mcp, pandas |
| `fusion.bootstrap`, `fusion.__init__` | anything | yes |

## File Structure

```
fusion/
├── __init__.py                     # public SDK (Settings, build_app, FusionApp, models, errors); build_app is lazy
├── bootstrap.py                    # build_app(), default_discovery() — the only module importing application + adapters
├── domain/
│   ├── models.py                   # TableRef, ColumnInfo, TableSchema, RowSet/RowStream, QueryResult, FetchPlan,
│   │                               # SourceCapabilities, TableSize, RefreshSpec, BackupInfo
│   ├── slices.py                   # Predicate, SliceSpec, LoadedSlice, SliceRegistry (what part of a table is loaded)
│   ├── query_shape.py              # TableUse, JoinEquality, QueryShape (per-table columns/predicates/join keys)
│   ├── policy.py                   # MaterializationPolicy, TargetPlan, QueryPlan, SemiJoinSpec, refusal messages
│   ├── catalog.py                  # SchemaCatalog (typed; tracks loaded tables and slices)
│   ├── identifiers.py              # identifier regexes, ALLOWED_AGG_FUNCS, MAX_RESULT_ROWS
│   ├── sql_text.py                 # literal/comment stripping, multi-statement + forbidden-function checks, cache normalizer
│   ├── views.py                    # ViewSpec, parse_refresh_interval, PRIORITY_ORDER
│   └── errors.py                   # FusionError hierarchy (+ BackupError, CircuitOpenError)
├── ports/                          # DataSource/PushdownCapable/SourceFactory/DatabaseDiscovery, AnalyticsStore,
│                                   # SqlValidator/SqlAnalyzer, QueryCache, Scheduler
├── application/
│   ├── settings.py                 # Settings (frozen dataclass), from_env(), validate()
│   ├── planner.py                  # FetchPlanner: FetchPlan (pushdown) + QueryPlan (reuse/full/slice/semi-join/refuse)
│   ├── sources.py                  # SourceService: connect/ensure_loaded/ensure_slices/evict/incremental refresh
│   ├── semijoin.py                 # SemiJoinExecutor: fetch a big table by the keys a small one holds
│   ├── query.py                    # QueryService: validate → cache → plan → pushdown | slices → rewrite → execute
│   ├── views.py                    # MaterializedViewService (loads referenced tables before CREATE TABLE AS)
│   ├── backup.py                   # BackupService (export dir for in-memory, snapshot file for file DBs)
│   ├── tools.py                    # ToolService: the 10 tools + execute() dispatch
│   ├── tool_schemas.py             # TOOL_DEFINITIONS, TOOL_NAMES, get_openai_tools(), get_mcp_tools()
│   └── app.py                      # FusionApp container + close()
├── adapters/
│   ├── outbound/
│   │   ├── duckdb_store.py         # DuckDBStore: lock, security latch, Arrow ingest, restore re-applies settings
│   │   ├── sqlglot_policy.py       # SqlglotValidator (allows exp.Query), SqlglotAnalyzer
│   │   ├── memory_cache.py         # MemoryQueryCache (LRU + TTL)
│   │   ├── threading_scheduler.py  # ThreadingScheduler
│   │   ├── registry.py             # SourceRegistry, default_registry() ("warp")
│   │   └── warp/                   # circuit_breaker, connection_pool, http (transport + client + SSRF),
│   │                               # capabilities (/info), streams (Arrow/NDJSON/paged), source, discovery
│   └── inbound/
│       ├── rest/                   # app.py (create_app), routes.py, schemas.py, rate_limit.py, middleware/{auth,logging}.py
│       ├── mcp/server.py           # create_mcp_server(fusion) on mcp 2.x MCPServer
│       ├── cli/                    # common.py, rest_main.py, mcp_main.py
│       └── sdk/formats.py          # to_dataframe, rowset_from_dataframe, to_arrow
└── observability/logging.py        # JSON/text formatters, setup_logging(settings, stream)
tests/
├── conftest.py                     # app / app_with_data / app_lazy / e2e_app fixtures
├── fakes/                          # FakeDataSource, FakeSourceFactory, FakeWarpTransport (mini SQL), ManualScheduler
├── architecture/, domain/, ports/ (contract tests), adapters/, application/, inbound/, e2e/
└── test_demo.py, test_packaging.py
```

## Key Classes

- `FusionApp` — assembled application: `settings`, `catalog`, `store`, `cache`, `scheduler`, `sources`, `query`, `views`, `backup`, `tools`; `close()` stops timers and closes sources/store
- `ToolService` — the 10 LLM tools; `execute(name, arguments)` is the universal dispatch and turns errors into `{"error": ...}`
- `QueryService` — the SQL pipeline; `sql(query, use_cache, cache_ttl, auto_load, params)`
- `SourceService` — owns connected `DataSource`s; `ensure_loaded(refs)` materializes tables through the store
- `MaterializedViewService` — `mv_{name}` tables, scheduled refresh, `describe()`
- `BackupService` — timestamped backups with retention over the store's export/snapshot methods
- `DuckDBStore` — the only code that touches DuckDB; holds the `threading.Lock` and the `enable_external_access=FALSE` latch
- `WarpSource` — `DataSource` + `PushdownCapable` over `WarpHttpClient` (pool + circuit breaker); reads `/info` capabilities, typed `/schema`, and `fetch_slice` over Arrow/NDJSON/paged reads
- `SliceSpec` / `SliceRegistry` — which columns and rows of a table are materialized, and whether a loaded slice covers a new request
- `MaterializationPolicy` — the row budgets (`full_load_max_rows`, `slice_max_rows`, `slice_budget_rows`, `semi_join_max_keys`) and the refusal messages
- `SqlglotValidator` / `SqlglotAnalyzer` — guardrails and table extraction / prefix stripping
- `Settings` — all configuration; `Settings.from_env()` is the only place the environment is read

## Conventions

- **Dependency rule first.** Domain/ports/application never import third-party code; adapters implement ports; only `bootstrap` composes them. Run `uv run pytest tests/architecture` after moving code.
- **All DuckDB access goes through `AnalyticsStore`.** The adapter holds the lock; services never see a connection.
- **`create_table_as` is the only non-SELECT path** and is reachable only from `MaterializedViewService` for `mv_*` tables.
- **Guardrails are mandatory for user SQL:** `SqlValidator.validate()` runs before any query; only `exp.Query` statements pass, forbidden file/network functions are denied, and the store latch is the backstop.
- **Rows travel as `RowSet`** (columns + tuples). pandas/Arrow only inside adapters and `sdk/formats.py`.
- **Tool results are capped at 100 rows**; result dict keys are `columns, rows, row_count, truncated, execution_time_ms, from_cache`.
- **Identifiers from LLMs are regex-validated** and columns are checked against the catalog; `search_data` binds its value as a `?` parameter.
- **Pushdown** happens only for single-source queries whose tables are all unloaded and reference no `mv_*` table; a failed pushdown falls back to local execution, and a Warp that refuses raw SQL (403) turns it off for that source.
- **Slices never change a result.** Only table names are rewritten; the original WHERE and ON clauses stay, predicates are pushed only when they are an AND of literal comparisons on one table (never OR/NOT/functions/parameters, never the null side of an outer join), and `SliceSpec.subsumes` is conservative — an unprovable match refetches.
- **A table is "loaded" only when all of it is in the store.** Partial slices live beside it in the registry; a slice cut short by the source is `complete=False` and never answers a narrower query.
- **Refusals are actionable.** When a table is too big to load and the query gives nothing to narrow it by, `QueryError` names the table, its estimated size, the limit and the four ways forward.
- **Materialized views are `mv_{name}` tables**; cross-source federation prefixes tables with the source name (`mydb.orders`).
- **Tests use fakes, not mocks:** `FakeSourceFactory` (config `tables`, `pushdown`) for sources, `ManualScheduler.tick()` for timers, `FakeWarpTransport` for the real `WarpSource`.
- **Version** lives only in `fusion/__init__.py` (`__version__`); pyproject reads it dynamically and `tests/test_packaging.py` checks the CHANGELOG heading.
