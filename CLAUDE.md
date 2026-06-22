# Fusion OLAP Engine

DuckDB-powered in-memory analytics engine — LLM tool for data access via MCP, REST API, and OpenAI Function Calling.

## Project Overview

- **Language**: Python 3.10+
- **Core dependency**: DuckDB 1.2+
- **Data source**: [Warp](https://github.com/yasinyaman/warp) REST API (PostgreSQL/MySQL)

## Commands

- Install: `pip install -e ".[all]"`
- Test: `pytest tests/ -v`
- Demo: `python -m demo.demo`
- Lint: `ruff check fusion/`
- MCP Server: `fusion-mcp --warp-url http://localhost:8000 --database mydb`
- REST Server: `fusion-rest --warp-url http://localhost:8000 --auto-discover --port 9000`

## Architecture

```
Warp REST API --> WarpConnector --> DuckDB (in-memory)
(PostgreSQL/MySQL)                       |
                                    LLM --> Tool Layer --> Result
                                         (MCP / REST / SDK)
```

Three main layers:
1. **Data Source** — Warp REST API connector (auto-discovery, pagination, schema inference, query pushdown)
2. **DuckDB Core** — In-memory columnar store, materialized views, query cache, catalog manager, lazy loading
3. **LLM Tool Layer** — 10 tools (MCP Server + REST API + OpenAI Function Calling), ToolExecutor, SQL guardrails

## File Structure

```
fusion/
├── __init__.py              # Public API exports
├── engine.py                # OLAPEngine — main orchestration + pushdown routing
├── cache.py                 # QueryCache (LRU with TTL)
├── catalog.py               # SchemaCatalog — multi-source metadata
├── guardrails.py            # SQLGuardrails — blocks destructive SQL
├── result.py                # QueryResult — output format conversions
├── strategy.py              # FetchStrategy — smart table loading + pushdown eligibility
├── exceptions.py            # Custom exception hierarchy
├── connectors/
│   ├── __init__.py          # Connector registry (warp only)
│   ├── base.py              # BaseConnector (abstract, supports_pushdown property)
│   └── warp.py              # WarpConnector (Warp REST API client, pushdown capable)
├── tools/
│   ├── __init__.py          # Tool layer exports
│   ├── definitions.py       # 10 tool schemas (OpenAI + MCP format)
│   ├── executor.py          # ToolExecutor — routes tool calls to engine
│   ├── mcp_server.py        # MCP Server (stdio transport, FastMCP)
│   └── rest_server.py       # REST API Server (FastAPI, Swagger UI)
└── views/
    └── materialized.py      # MaterializedViewManager
```

## Key Classes

- `OLAPEngine` — Main DuckDB engine wrapper (connect sources, run SQL, caching, pushdown, tool helpers)
- `ToolExecutor` — Routes LLM tool calls to engine operations (10 tools)
- `WarpConnector` — REST API client for Warp (auto-discovery, pagination, schema inference, pushdown)
- `SQLGuardrails` — Blocks destructive SQL (DROP, DELETE, INSERT); allows only SELECT/CTE
- `FetchStrategy` — SQL AST parsing via sqlglot to determine which tables to load + pushdown eligibility
- `MaterializedViewManager` — Pre-computed aggregation views with auto-refresh
- `QueryCache` — LRU-based result caching with TTL
- `SchemaCatalog` — Multi-source metadata management
- `QueryResult` — Result wrapper with to_dataframe/to_markdown/to_json/to_csv

## Tools (10)

| Tool | Description |
|------|-------------|
| `list_sources` | Connected sources and tables |
| `describe_table` | Table schema (columns, types, row count) |
| `query_data` | Run analytical SQL on DuckDB (SELECT only) |
| `search_data` | Simple filter search on a table |
| `aggregate_data` | GROUP BY aggregation |
| `create_view` | Create materialized view |
| `list_views` | List materialized views |
| `refresh_view` | Refresh a materialized view |
| `load_table` | Explicitly load a table from source |
| `cache_stats` | Query cache statistics |

## Conventions

- Warp is the sole data source connector — connects via REST API to running Warp instance
- SQL guardrails must block all non-SELECT statements (critical for LLM safety)
- Tool results are capped at 100 rows to fit LLM context windows
- Cross-source federation: prefix tables with source name (e.g., `primary_db.orders`)
- Materialized views stored as `mv_{name}` tables in DuckDB
- DuckDB connection is protected with `threading.Lock` — all `_conn.execute()` calls must hold `_lock`
- `execute_raw()` bypasses guardrails — only for internal use (MV creation, schema setup)
- Tool definitions exist in dual format: OpenAI Function Calling + MCP
- MCP server uses stdio transport via FastMCP
- `ToolExecutor.execute()` is the universal dispatch — accepts tool_name + arguments dict
- Identifier validation via regex prevents SQL injection in tool parameters
- Query pushdown sends SQL directly to source database when: single source, tables not loaded, connector supports it
- Lazy loading: `connect_source()` only fetches metadata; data loaded on-demand when queries reference tables
