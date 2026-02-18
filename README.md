# Fusion

**DuckDB-powered in-memory analytics engine with LLM tool support.**

Fusion connects to PostgreSQL/MySQL databases via [Warp](https://github.com/yasinyaman/warp) REST API, loads data into DuckDB for fast columnar analytics, and exposes 10 tools for LLMs through MCP and OpenAI Function Calling.

## Features

- **10 LLM Tools** — `list_sources`, `describe_table`, `query_data`, `search_data`, `aggregate_data`, `create_view`, `list_views`, `refresh_view`, `load_table`, `cache_stats`
- **Dual Format** — Tool definitions in both MCP (Model Context Protocol) and OpenAI Function Calling format
- **3 Access Layers** — MCP Server (stdio), REST API (FastAPI/HTTP), Python SDK
- **Query Pushdown** — Routes queries directly to source databases when possible, avoiding unnecessary data transfer
- **Lazy Loading** — Only fetches table data from sources when actually referenced in queries
- **SQL Guardrails** — Blocks destructive SQL (DROP, DELETE, INSERT) to protect data integrity
- **LRU Cache** — Query result caching with configurable TTL for millisecond response times
- **Materialized Views** — Pre-computed aggregation tables with scheduled auto-refresh
- **Cross-Source Federation** — JOIN across multiple databases (PostgreSQL + MySQL) in a single query
- **Auto-Discovery** — Automatically discovers all databases and tables from Warp

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. Data Source Layer                                                       │
│  ┌──────────────┐    REST     ┌─────────────────┐                          │
│  │ PostgreSQL   │ ──────────► │                 │                          │
│  │ MySQL        │             │ WarpConnector    │  auto-discovery           │
│  └──────────────┘             │ (query pushdown) │  pagination, schema       │
│       Warp REST API           └────────┬────────┘                          │
└────────────────────────────────────────┼──────────────────────────────────┘
                                          │
┌─────────────────────────────────────────▼──────────────────────────────────┐
│  2. DuckDB Core Layer                                                       │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ OLAPEngine                                                            │  │
│  │  • DuckDB (in-memory, columnar)   • QueryCache (LRU + TTL)            │  │
│  │  • SchemaCatalog (multi-source)   • MaterializedViewManager           │  │
│  │  • FetchStrategy (lazy load)      • SQLGuardrails (SELECT only)       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────┬──────────────────────────────────┘
                                          │
┌─────────────────────────────────────────▼──────────────────────────────────┐
│  3. LLM Tool Layer                                                          │
│  ┌─────────────┐  ┌──────────────────┐  ┌─────────────────┐                 │
│  │ ToolExecutor│  │ 10 tools         │  │ MCP / REST / SDK│                 │
│  │ (dispatch)  │─►│ query_data, etc. │─►│ → LLM → Result  │                 │
│  └─────────────┘  └──────────────────┘  └─────────────────┘                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Installation

```bash
pip install -e .
```

Optional dependencies:

```bash
pip install -e ".[mcp]"     # MCP Server support
pip install -e ".[rest]"    # REST API (FastAPI + uvicorn)
pip install -e ".[dev]"     # Development (pytest, ruff, mypy)
pip install -e ".[all]"     # Everything
```

## Quick Start

### Python SDK

```python
from fusion import OLAPEngine

engine = OLAPEngine(memory_limit="4GB")
engine.connect_source("mydb", {
    "type": "warp",
    "base_url": "http://localhost:8000",
    "database": "mydb",
})

executor = engine.get_tool_executor()

# Discover available data
sources = executor.list_sources()

# Run an analytical query (auto-loads referenced tables)
result = executor.query_data("SELECT * FROM mydb.orders LIMIT 10")

# Aggregate data
agg = executor.aggregate_data(
    table="mydb.orders",
    group_by="status",
    agg_column="amount",
    agg_func="SUM",
)

# Create a materialized view
executor.create_view(
    name="daily_revenue",
    sql="SELECT status, SUM(amount) as total FROM mydb.orders GROUP BY status",
    refresh="hourly",
)
```

### MCP Server (Claude Desktop / Cursor)

```bash
fusion-mcp --warp-url http://localhost:8000 --database mydb
```

Configure in `claude_desktop_config.json`:

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

Auto-discover all databases:

```bash
fusion-mcp --warp-url http://localhost:8000 --auto-discover
```

### REST API

```bash
fusion-rest --warp-url http://localhost:8000 --auto-discover --port 9000
```

Swagger UI at `http://localhost:9000/docs`. Key endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sources` | GET | List connected sources and tables |
| `/tables/{source.table}/schema` | GET | Table schema details |
| `/query` | POST | Execute analytical SQL query |
| `/search` | POST | Filter search on a table |
| `/aggregate` | POST | GROUP BY aggregation |
| `/views` | GET/POST | List or create materialized views |
| `/views/{name}/refresh` | POST | Refresh a materialized view |
| `/tables/{source.table}/load` | POST | Explicitly load a table |
| `/cache/stats` | GET | Cache statistics |
| `/tools/{tool_name}` | POST | Generic tool dispatch |

### OpenAI Function Calling

```python
from fusion import get_openai_tools, OLAPEngine

engine = OLAPEngine()
engine.connect_source("mydb", {"type": "warp", "base_url": "http://localhost:8000"})
executor = engine.get_tool_executor()

# Get tool definitions for OpenAI Chat Completions API
tools = get_openai_tools()

# When the LLM makes a tool call:
result = executor.execute("query_data", {"sql": "SELECT ..."})
```

## Tools

| Tool | Description |
|------|-------------|
| `list_sources` | Connected sources and tables with row counts |
| `describe_table` | Table schema (columns, types, row count) |
| `query_data` | Run analytical SQL on DuckDB (SELECT only, max 100 rows) |
| `search_data` | Filter search on a table (exact match or LIKE with %) |
| `aggregate_data` | GROUP BY aggregation (SUM, AVG, COUNT, MIN, MAX) |
| `create_view` | Create a materialized view from a SELECT query |
| `list_views` | List materialized views with refresh schedule |
| `refresh_view` | Manually refresh a materialized view |
| `load_table` | Explicitly load a table from source into DuckDB |
| `cache_stats` | Query cache hit rate, entry count, memory usage |

## Warp Setup

Fusion uses [Warp](https://github.com/yasinyaman/warp) as its data source gateway:

```bash
git clone https://github.com/yasinyaman/warp.git
cd warp
docker compose up -d
```

Warp provides a REST API that federates access to PostgreSQL and MySQL databases.

## Project Structure

```
fusion/
├── __init__.py              # Public API exports
├── engine.py                # OLAPEngine — main orchestration
├── cache.py                 # QueryCache (LRU + TTL)
├── catalog.py               # SchemaCatalog — multi-source metadata
├── guardrails.py            # SQLGuardrails — blocks destructive SQL
├── result.py                # QueryResult — format conversions
├── strategy.py              # FetchStrategy — smart table loading
├── exceptions.py            # Custom exception hierarchy
├── connectors/
│   ├── base.py              # BaseConnector (abstract)
│   └── warp.py              # WarpConnector (Warp REST API)
├── tools/
│   ├── definitions.py       # 10 tool schemas (OpenAI + MCP)
│   ├── executor.py          # ToolExecutor — routes tool calls
│   ├── mcp_server.py        # MCP Server (stdio transport)
│   └── rest_server.py       # REST API Server (FastAPI)
└── views/
    └── materialized.py      # MaterializedViewManager
```

## Development

```bash
pip install -e ".[all]"
pytest tests/ -v           # 236 tests
ruff check fusion/         # Lint
python -m demo.demo        # Demo with synthetic data
```

## Requirements

- Python 3.10+
- DuckDB 1.2+
- [Warp](https://github.com/yasinyaman/warp) (data source gateway)

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
