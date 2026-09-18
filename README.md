# Fusion

**DuckDB-powered in-memory analytics engine with LLM tool support.**

Fusion connects to PostgreSQL/MySQL databases through the [Warp](https://github.com/yasinyaman/warp) REST API, lazily loads the tables a query needs into DuckDB, and exposes 10 analytics tools to LLMs over MCP, a REST API and a Python SDK.

## Features

- **10 LLM tools** — `list_sources`, `describe_table`, `query_data`, `search_data`, `aggregate_data`, `create_view`, `list_views`, `refresh_view`, `load_table`, `cache_stats`
- **Three access layers** — MCP server (stdio), REST API (FastAPI), Python SDK; tool definitions in OpenAI function-calling and MCP formats
- **Lazy loading** — connecting a source fetches metadata only; tables are pulled on first use
- **Smart transfer** — a table too big for memory is read one slice at a time: only the columns and rows a query touches, streamed as Arrow, reused by later queries, and refused with concrete advice when even that is too much
- **Query pushdown** — single-source queries on unloaded tables run on the source database
- **Cross-source federation** — JOIN PostgreSQL and MySQL tables in one DuckDB query
- **SQL guardrails** — only read-only queries (SELECT, CTEs, UNION/INTERSECT/EXCEPT) reach DuckDB; file/network functions are denied and DuckDB's external access is latched off
- **Query cache, materialized views, backups** — LRU cache with TTL, `mv_*` tables with scheduled refresh, timestamped backups with retention
- **Resilient Warp client** — pooled connections with retry/backoff behind a circuit breaker, SSRF-guarded URLs
- **Hexagonal architecture** — pure domain and application layers, adapters for DuckDB, sqlglot, Warp, FastAPI and MCP; every port has an in-memory fake for tests

## Architecture

```
                 inbound adapters                       outbound adapters
   ┌──────────┐ ┌──────────┐ ┌──────────┐      ┌─────────────┐ ┌─────────────┐
   │ REST API │ │ MCP srv  │ │ CLI/SDK  │      │ DuckDBStore │ │ WarpSource  │
   └────┬─────┘ └────┬─────┘ └────┬─────┘      └──────┬──────┘ └──────┬──────┘
        │            │            │                   │               │
        ▼            ▼            ▼            AnalyticsStore     DataSource
   ┌──────────────────────────────────────┐   ┌────────────────────────────────┐
   │ application  (FusionApp)             │   │ ports (Protocols)              │
   │  ToolService · QueryService          │◄──┤  AnalyticsStore · DataSource   │
   │  SourceService · MaterializedView    │   │  SqlValidator · SqlAnalyzer    │
   │  BackupService · FetchPlanner        │   │  QueryCache · Scheduler        │
   └───────────────────┬──────────────────┘   └────────────────────────────────┘
                       ▼
   ┌──────────────────────────────────────┐   sqlglot policy · memory cache
   │ domain  (pure Python)                │   threading scheduler · Warp HTTP
   │  TableRef · RowSet · QueryResult     │   (pool + circuit breaker)
   │  SchemaCatalog · guardrail text rules│
   └──────────────────────────────────────┘
```

Dependencies point inward: `domain` imports nothing, `ports` only `domain`, `application` only `domain` + `ports`. Adapters implement the ports, and `fusion.bootstrap.build_app()` wires them together. An architecture test enforces the rule.

## Installation

Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or pip):

```bash
uv sync --all-extras          # development checkout
pip install "fusion[all]"     # everything
pip install "fusion[rest]"    # REST API (FastAPI + uvicorn)
pip install "fusion[mcp]"     # MCP server
pip install "fusion[pandas]"  # DataFrame conversions
```

The core package depends only on `duckdb`, `pyarrow`, `requests` and `sqlglot`.

## Quick Start

### Python SDK

```python
from fusion import Settings, build_app

app = build_app(Settings(memory_limit="4GB"))          # or Settings.from_env()
app.sources.connect("mydb", {
    "type": "warp",
    "base_url": "http://localhost:8000",
    "database": "mydb",
})

app.tools.list_sources()                                  # what is available (metadata only)
app.tools.query_data("SELECT * FROM mydb.orders LIMIT 10")  # loads mydb.orders on first use
app.tools.aggregate_data("mydb.orders", "status", "amount", "SUM")
app.views.create("daily_revenue",
                 "SELECT status, SUM(amount) AS total FROM mydb.orders GROUP BY status",
                 refresh="hourly")

result = app.query.sql("SELECT COUNT(*) AS n FROM mydb.orders")  # QueryResult
result.to_records(); result.to_markdown(); result.to_json()

app.close()
```

`build_app` accepts replacement adapters (`store=`, `cache=`, `scheduler=`, `source_factory=`, `validator=`, `analyzer=`), which is how the tests plug in fakes.

### MCP Server (Claude Desktop / Cursor)

```bash
fusion-mcp --warp-url http://localhost:8000 --database mydb
fusion-mcp --warp-url http://localhost:8000 --auto-discover
```

```json
{
  "mcpServers": {
    "fusion": {
      "command": "fusion-mcp",
      "args": ["--warp-url", "http://localhost:8000", "--database", "mydb"]
    }
  }
}
```

### REST API

```bash
fusion-rest --warp-url http://localhost:8000 --auto-discover --port 9000
```

Swagger UI at `http://localhost:9000/docs`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health`, `/readiness` | GET | Liveness / readiness |
| `/sources` | GET | Connected sources and tables |
| `/tables/{source.table}/schema` | GET | Table schema |
| `/query` | POST | Analytical SQL (`{"sql": ...}`) |
| `/search` | POST | Filter search on a table |
| `/aggregate` | POST | GROUP BY aggregation |
| `/views` | GET/POST | List or create materialized views |
| `/views/{name}/refresh` | POST | Refresh a materialized view |
| `/tables/{source.table}/load` | POST | Explicitly load a table |
| `/cache/stats` | GET | Cache statistics |
| `/tools`, `/tools/{tool_name}` | GET/POST | Tool definitions and generic dispatch |
| `/backup/list`, `/backup/create`, `/backup/stats` | GET/POST/GET | Backups |

Configuration comes from `FUSION_*` / `WARP_URL` environment variables (see `.env.example`); in production an API key is mandatory and requests are rate-limited per key.

### OpenAI Function Calling

```python
from fusion import Settings, build_app, get_openai_tools

app = build_app(Settings())
app.sources.connect("mydb", {"type": "warp", "base_url": "http://localhost:8000"})

tools = get_openai_tools()                      # pass to the Chat Completions API
result = app.tools.execute("query_data", {"sql": "SELECT ..."})   # when the model calls a tool
```

## Tools

| Tool | Description |
|------|-------------|
| `list_sources` | Connected sources and tables with row counts, source-side estimates, load state and loaded slices |
| `describe_table` | Table schema (columns, types, row count) |
| `query_data` | Run analytical SQL on DuckDB (read-only, max 100 rows) |
| `search_data` | Filter search (exact match or LIKE with %) |
| `aggregate_data` | GROUP BY aggregation (SUM, AVG, COUNT, MIN, MAX) |
| `create_view` | Create a materialized view from a SELECT query |
| `list_views` | List materialized views with refresh schedule |
| `refresh_view` | Manually refresh a materialized view |
| `load_table` | Load a table, or just the slice `where` / `columns` describe |
| `cache_stats` | Query cache hit rate and entry count |

Every tool returns a JSON-serializable dict; failures come back as `{"error": "..."}`.

## Working with tables larger than memory

Fusion is an in-memory engine, so it will not load a table it cannot hold.
Instead of guessing, it reads what a query actually needs:

```python
# Only the matching rows and columns travel; the slice stays for later queries.
app.query.sql("SELECT id, total FROM shop.orders WHERE status = 'paid'")

# Or load a slice explicitly
app.tools.load_table("shop.orders", where="status = 'paid'", columns=["id", "total"])
```

A query with nothing to narrow a big table by is refused, with its estimated
size and the ways forward (add a WHERE, select fewer columns, join it to a
small table on an equality key, or raise the limit). A join between a huge
table and a small one fetches only the rows whose key appears in the small
one.

Slicing needs a source that can filter; Warp does. With Warp >= 0.10 the
rows arrive as an Arrow IPC stream with their real types, and table sizes
come from `GET /schema` without counting rows. Against Warp 0.9 everything
still works through the paged list endpoint.

| Setting | Default | Meaning |
|---|---|---|
| `FUSION_FULL_LOAD_MAX_ROWS` | `500000` | Largest table loaded whole when a query has no usable filter |
| `FUSION_SLICE_MAX_ROWS` | `500000` | Largest single slice |
| `FUSION_SLICE_BUDGET_ROWS` | `2000000` | Rows kept across all slices before the least used are evicted |
| `FUSION_SEMI_JOIN_MAX_KEYS` | `50000` | Most join keys passed to the source |
| `FUSION_IN_CHUNK_SIZE` | `1000` | Keys per request when passing them |
| `FUSION_REFRESH_CONFIG` | *(none)* | Per-table incremental refresh, as JSON |

## Warp Setup

```bash
git clone https://github.com/yasinyaman/warp.git
cd warp
docker compose up -d
```

Fusion authenticates with `WARP_API_KEY`, sent in the header Warp expects
(`X-API-Key` by default, `FUSION_WARP_API_KEY_HEADER` to change it). Raw SQL
pushdown is used only when Warp advertises it, so a Warp with
`enable_raw_query: false` (the default, and mandatory in production) is fully
supported through slices.

Incremental refresh keeps a table up to date without re-reading it:

```bash
export FUSION_REFRESH_CONFIG='{"shop.orders": {"watermark_column": "updated_at", "key_columns": ["id"]}}'
```

## Project Structure

```
fusion/
├── __init__.py                 # public SDK: Settings, build_app, FusionApp, models, errors
├── bootstrap.py                # composition root (build_app, default_discovery)
├── domain/                     # pure Python: models, catalog, slices, query_shape, policy, identifiers, sql_text, views, errors
├── ports/                      # Protocols: DataSource, AnalyticsStore, SqlValidator/Analyzer, QueryCache, Scheduler
├── application/                # Settings, FetchPlanner, Source/Query/View/Backup/Tool services, SemiJoinExecutor, FusionApp
├── adapters/
│   ├── outbound/               # duckdb_store, sqlglot_policy, memory_cache, threading_scheduler, registry
│   │   └── warp/               # http (pool + circuit breaker + SSRF guard), capabilities, streams, source, discovery
│   └── inbound/
│       ├── rest/               # FastAPI app, routes, middleware (auth, logging), rate limit
│       ├── mcp/                # MCPServer adapter
│       ├── cli/                # fusion-rest, fusion-mcp entry points
│       └── sdk/                # pandas / Arrow conversions (optional)
└── observability/              # logging setup and formatters
tests/                          # domain, ports (contract tests), adapters, application, inbound, e2e
demo/demo.py                    # in-process demo over synthetic data
```

## Development

```bash
uv sync --all-extras
uv run pytest                    # 900+ tests, coverage gate 80%
uv run ruff check fusion tests demo && uv run ruff format --check fusion tests demo
uv run mypy fusion               # strict on domain/ports/application
uv run python -m demo.demo       # demo with synthetic data (--scale 0.1 for a quick run)
```

## Requirements

- Python 3.12+
- DuckDB 1.5+
- [Warp](https://github.com/yasinyaman/warp) (data source gateway)

## License

Apache 2.0 — see [LICENSE](LICENSE).
