# Changelog

All notable changes to Fusion OLAP Engine.

## [Unreleased]

### Security
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
