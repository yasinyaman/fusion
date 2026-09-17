# Changelog

All notable changes to Fusion OLAP Engine.

## [1.1.0] - 2026-09-18

Smart transfer: a source table no longer has to fit in memory to be useful.
Fusion reads the part of it a query actually touches, and says so plainly
when even that is too much.

### Added
- **Slices.** A query's own columns and literal conditions become a
  `SliceSpec`; only the matching rows and columns are fetched and they land
  in their own table (`source.table__s_<hash>`). The original WHERE and ON
  clauses stay in the rewritten query, so a slice can only narrow what is
  scanned, never change a result. A later query whose needs are contained in
  a loaded slice reuses it.
- **Row budgets and actionable refusals.** `FUSION_FULL_LOAD_MAX_ROWS`,
  `FUSION_SLICE_MAX_ROWS` and `FUSION_SLICE_BUDGET_ROWS` decide what may be
  loaded; a table that is too big without a filter is refused with its
  estimated size and the four ways to proceed. Slices past the budget are
  evicted least-recently-used first.
- **Semi-joins.** A huge table joined to a small one is fetched by the keys
  the small one holds, in `FUSION_IN_CHUNK_SIZE` chunks, up to
  `FUSION_SEMI_JOIN_MAX_KEYS` distinct values.
- **Arrow streaming ingest.** Warp 0.10's `/{table}/export` is read as an
  Arrow IPC stream and handed to DuckDB batch by batch (NDJSON and paged
  JSON are the fallbacks), so memory no longer scales with table size.
  `AnalyticsStore` gains `materialize_stream`, `append_stream`, `upsert`,
  `delete_where_in`, `table_size` and `rename_table`; a staged load is
  published with a rename, so a reload is atomic.
- **Typed schema and size estimates.** Warp 0.10's `/schema` provides column
  types and planner row estimates, so the size of a table is known before
  anything is fetched. `list_sources` reports `row_estimate` and the loaded
  slices of each table.
- **Incremental refresh.** `FUSION_REFRESH_CONFIG` (JSON, keyed by
  `source.table`, with `watermark_column` and optional `key_columns`) tops a
  table up instead of re-fetching it, replacing rows by key. Refreshing data
  or rebuilding a materialized view now clears the query cache.
- `load_table(table, where=..., columns=...)` loads a slice explicitly, over
  the SDK, MCP and `POST /tables/{table}/load`. `search_data` on a table too
  big to load fetches just the matching rows.

### Fixed
- Fusion sent `Authorization: Bearer`; Warp reads `X-API-Key`. The header is
  now correct and configurable (`FUSION_WARP_API_KEY_HEADER`), and
  `WARP_API_KEY` is passed to every Warp source and to `--auto-discover`.
- The circuit breaker counted every failure, so five refused pushdowns
  (HTTP 403) opened it and blocked all traffic for a minute. Only network
  errors, 5xx, 408 and 429 count now; a Warp answering 4xx is healthy.
- Raw SQL is only attempted when Warp advertises it, and a 403 turns
  pushdown off for that source instead of being retried on every query.
- `query()` sent `params` as a list; Warp binds named `:name` parameters
  from an object.
- A single-database Warp 0.9 serves un-prefixed table routes; the client now
  detects that on a 404 and keeps the working layout.

### Changed
- `list_sources` gains `row_estimate` and `slices` per table; `load_table`
  gains `where` and `columns` and returns the row count.
- A table is "loaded" only when all of it is in the store. Partial slices
  are tracked separately and never answer a query they do not cover.

## [1.0.0] - 2026-09-17

### Breaking
- **Hexagonal architecture.** The package is now `domain / ports / application /
  adapters` with `fusion.bootstrap.build_app(settings) -> FusionApp` as the
  composition root. `OLAPEngine`, `ToolExecutor`, `engine.get_tool_executor()`,
  `execute_raw()`, `fusion.config` and the `fusion.{engine,cache,catalog,
  guardrails,result,strategy,backup,connectors,tools,views,middleware,utils}`
  modules are gone. Use `app.sources / app.query / app.views / app.backup /
  app.tools` instead; tool names, arguments and result shapes are unchanged.
- **Python >= 3.12**; CI runs 3.12 / 3.13 / 3.14.
- **pandas and tabulate left the core dependencies.** Rows travel as the domain
  `RowSet`; DuckDB ingest uses pyarrow. `fusion[pandas]` provides
  `to_dataframe()` / `rowset_from_dataframe()`; `QueryResult.to_markdown()` is
  pure Python and `to_csv()` returns a string.
- **In-memory backups are `EXPORT DATABASE` directories** (`fusion_backup_<ts>/`)
  and require `FUSION_DUCKDB_EXTERNAL_ACCESS=true`; file databases are
  snapshotted to `.duckdb` files. `/backup/create` returns `409` when backups
  are disabled or impossible.
- `FUSION_ALLOWED_HOSTS` and `FUSION_RATE_LIMIT_BURST` (never used) were removed.
- Entry points moved to `fusion.adapters.inbound.cli.{rest_main,mcp_main}:main`
  (the `fusion-rest` / `fusion-mcp` commands are unchanged).

### Fixed
- Demo crashed under the DuckDB external-access latch (replacement scan);
  it now runs on `FrameSource` + `build_app` (`tests/test_demo.py`).
- In-memory backups never worked (`current_database()` returns `memory`,
  not `:memory:`) and `EXPORT DATABASE` directories were not listed
  (`tests/application/test_backup.py::TestInMemory`).
- Restoring a backup reopened DuckDB **without** the security latch and
  settings; `DuckDBStore.restore_from` re-applies them
  (`tests/adapters/outbound/test_duckdb_store.py::TestBackup::test_restore_from_reapplies_security_latch`).
- Guardrails rejected `UNION` / `INTERSECT` / `EXCEPT` and parenthesised
  selects; any `exp.Query` is now allowed while DDL/DML/commands stay blocked
  (`tests/adapters/outbound/test_sqlglot_policy.py::TestSetOperations`).
- The query cache upper-cased string literals, so `'alice'` and `'ALICE'`
  shared an entry; quoted segments are now kept verbatim in the key
  (`tests/adapters/outbound/test_memory_cache.py::test_literal_case_is_significant`).
- `create_view` on a table that had never been queried failed with "table
  does not exist"; the view service loads referenced tables first
  (`tests/application/test_views.py::TestLazyLoad`).
- The Dockerfile's exec-form `CMD` passed a literal `${WARP_URL:-...}` to
  `fusion-rest`; settings now come from the environment
  (`tests/test_packaging.py::test_dockerfile_cmd_has_no_unexpanded_variables`).

### Added
- Ports with in-memory fakes and contract tests (`tests/ports`), an
  architecture test that enforces the dependency rule, and e2e scenarios over
  a fake Warp HTTP transport (541 tests, coverage gate 80%).
- `CircuitBreaker` and `ConnectionPool` are now wired into the Warp HTTP
  transport (retry/backoff behind a per-source breaker, no redirects).
- `Settings.from_env()` replaces the import-time `Config` singleton; the MCP
  server now honours memory/thread/ingest settings and logs to stderr.
- REST: constant-time API-key comparison, rate limiting enforced for every
  route via `SlowAPIMiddleware`, sanitized `X-Request-ID`, `409` for backup
  errors, no module-level globals (`create_app(fusion, settings)`).
- Docker image built with `uv sync --frozen` from `uv.lock` on Python 3.13;
  `docker-compose.yml` without the obsolete `version` key.

### Changed
- Dependencies: duckdb 1.5, pyarrow 25, sqlglot 30, mcp 2 (`MCPServer`),
  fastapi 0.141, pytest 9, mypy 2, ruff 0.16; `uv.lock` regenerated.
- Materialized-view references (`mv_*`) now disable pushdown for the query.
- Unqualified table names resolve in source registration order (deterministic).

### Security (carried over from the unreleased hardening work)
- **DuckDB external access disabled by default** — `enable_external_access=FALSE`
  on every engine connection blocks `read_csv`/`read_parquet`/`glob`/`ATTACH`/
  `COPY` against the local filesystem and network (local file exfiltration /
  SSRF). Opt back in with `FUSION_DUCKDB_EXTERNAL_ACCESS=true` (needed for
  `EXPORT DATABASE` backups). Table loading now uses explicit DataFrame
  registration so it keeps working under the latch.
- **Guardrail function denylist** — blocks dangerous DuckDB functions
  (`read_csv`, `glob`, `install`, `load`, …) even inside an otherwise-valid
  `SELECT`, on top of the existing statement-type allowlist.
- **Production fail-fast** — the REST server refuses to start in production when
  `FUSION_API_KEY` is empty or a placeholder (previously auth silently disabled),
  or when CORS origins contain `*`.
- **Parameterized tool queries** — `search_data` now binds the filter value as a
  query parameter (`?`) instead of interpolating it into the SQL string.
  `search_data`/`aggregate_data` also validate table and column names against the
  catalog (defense-in-depth over the identifier regex). `QueryCache` keys on
  bound params so distinct values no longer collide.
- **Warp SSRF guard** — `base_url` is restricted to `http(s)` schemes, cloud
  metadata / link-local hosts (e.g. `169.254.169.254`) are blocked, and HTTP
  redirects are not followed. Loopback/private ranges stay allowed for normal
  local/Docker deployments.
- **pyarrow CVE fix** — bumped to `>=23.0.1` to resolve PYSEC-2026-113, found by
  `pip-audit` against the new lockfile.
- **`/debug/config` hardened** — disabled in production by default (returns 404);
  force-enable with `FUSION_DEBUG_ENDPOINTS=true`.
- **Per-API-key rate limiting** — the limiter now buckets on a hash of the
  `X-API-Key` header (falling back to client IP), resisting `X-Forwarded-For`
  spoofing and unfair throttling of clients behind a shared NAT/proxy.

### Added
- **CI pipeline** (`.github/workflows/ci.yml`) — against the locked deps:
  ruff + pytest on Python 3.10/3.11/3.12, a dedicated **mypy** type-check job
  (blocking), and a `pip-audit` dependency scan.
- **Clean type checking** — `mypy fusion/` now passes (was 28 errors); config
  added under `[tool.mypy]`.
- **Coverage gate** — `pytest-cov` with a `fail_under = 75` ratchet (currently
  ~78%); enforced in CI.
- **More tests** — HTTP-contract tests for `WarpConnector` (via `responses`)
  plus unit tests for the circuit breaker, connection pool, and auth middleware
  (328 tests total).
- **DuckDB resource guards** — `FUSION_MAX_TEMP_DIRECTORY_SIZE` caps on-disk spill
  and `FUSION_MAX_INGEST_ROWS` caps rows pulled from a source when materializing a
  table (OOM guard on the non-pushdown path; truncation is logged).

### Changed
- Renamed committed `.env.production` to `.env.production.example` and ignore
  real `.env.production`, to prevent leaking secrets.
- **Dependency pinning** — added upper version bounds to all dependencies and a
  committed `uv.lock` for reproducible installs; added `pip-audit` to the `dev`
  extra for CVE scanning.
- Removed pre-existing unused imports / variables so `ruff check` is clean
  across the repo.

## [0.5.0] - 2026-02-18

### Added - Production-Ready Features

#### Security
- **API Key Authentication** - X-API-Key header validation via `AuthMiddleware`
- **CORS Configuration** - Environment-based whitelist (no wildcard in production)
- **Config Management** - `fusion/config.py` with environment variable support
- **Identifier Validation** - SQL injection protection for table/column names

#### Reliability
- **Circuit Breaker** - `fusion/utils/circuit_breaker.py` prevents cascading failures
- **Connection Pooling** - `fusion/utils/connection_pool.py` with retry logic and exponential backoff
- **Graceful Shutdown** - SIGTERM/SIGINT handlers in REST server
- **Health Checks** - `/health` (liveness) and `/readiness` (validates connections)

#### Performance
- **Rate Limiting** - `slowapi` integration (100 req/min default, configurable)
- **Request Tracing** - X-Request-ID header tracking

#### Observability
- **Structured Logging** - JSON formatter (`fusion/utils/logger.py`)
- **Request/Response Logging** - `StructuredLoggingMiddleware` with timing
- **Debug Endpoints** - `/debug/config` shows current configuration

#### Data Management
- **Backup/Restore** - `fusion/backup.py` with automated scheduling
- **Backup API** - `/backup/create`, `/backup/list`, `/backup/stats` endpoints
- **Retention Policy** - Configurable backup cleanup (7 days default)

#### Deployment
- **Dockerfile** - Multi-stage build with non-root user
- **docker-compose.yml** - Complete stack with Warp, healthchecks, resource limits
- **.dockerignore** - Optimized image size
- **.env.production.example** - Production configuration template
- **DEPLOYMENT.md** - Comprehensive deployment guide

### Changed

- **REST Server** - Complete rewrite with production features
  - Lifespan events for startup/shutdown
  - Middleware stack (logging → auth → CORS)
  - Enhanced error handling
  - Config-driven configuration
  
- **Dependencies** - Added production packages
  - `slowapi>=0.1.9` for rate limiting
  - Existing: `requests`, `urllib3` for connection pooling

- **Logging** - Switched from basicConfig to structured logging
  - JSON format in production
  - Human-readable format in development
  - Request IDs for tracing

### Fixed

- **CORS Security** - Removed wildcard (`*`) origins in production
- **Memory Safety** - Added explicit resource cleanup in `engine.close()`

## [0.4.0] - Previous

### Initial Features

- DuckDB-powered in-memory analytics
- Warp REST API connector
- 10 LLM tools (MCP + OpenAI Function Calling)
- SQL guardrails
- Query caching (LRU + TTL)
- Materialized views
- Lazy loading and query pushdown
- Multi-source federation

---

**Versioning:** We use [Semantic Versioning](https://semver.org/).

**Legend:**
- `Added` - New features
- `Changed` - Changes to existing functionality
- `Deprecated` - Features marked for removal
- `Removed` - Removed features
- `Fixed` - Bug fixes
- `Security` - Security improvements
