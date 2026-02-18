# Changelog

All notable changes to Fusion OLAP Engine.

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
- **.env.production** - Production configuration template
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
