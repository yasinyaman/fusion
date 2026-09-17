# Fusion OLAP Engine - Deployment Guide

Production deployment guide for Fusion 1.x.

## Production Features

### Security
- **API key authentication** — `X-API-Key` header, constant-time comparison; mandatory in production (the server refuses to start without a real key)
- **CORS whitelist** — explicit origins only (no `*` in production)
- **SQL guardrails** — AST allowlist (read-only queries), forbidden file/network functions, multi-statement detection
- **DuckDB security latch** — `enable_external_access=FALSE` by default; the latch is re-applied after every reconnect/restore
- **Identifier validation** — regex + catalog checks for table/column names; parameter binding for search values
- **SSRF guard** — Warp URLs must be http(s), never link-local/metadata hosts; redirects are not followed

### Reliability
- **Circuit breaker** — Warp calls are guarded per source (`warp:<source>`)
- **Connection pooling with retry/backoff** — transient HTTP errors are retried; the last response's status is surfaced
- **Graceful shutdown** — uvicorn handles SIGTERM/SIGINT; sources, timers and the store are closed afterwards
- **Health checks** — `/health` (liveness) and `/readiness` (at least one source connected)

### Performance
- **Rate limiting** — per API key (hashed), falling back to client IP; default `100/minute`
- **Query cache** — LRU with TTL
- **Lazy loading** and **query pushdown**

### Observability
- **Structured JSON logging** — `FUSION_LOG_FORMAT=json`
- **Request tracing** — `X-Request-ID` (echoed, sanitized)
- **Introspection** — `/cache/stats`, `/backup/stats`, `/debug/config` (off in production unless `FUSION_DEBUG_ENDPOINTS=true`)

### Data Management
- **Automated backups** — periodic, with retention
- **Backup API** — `/backup/list`, `/backup/create`, `/backup/stats`

## Quick Start (Docker)

### 1. Configure Environment

```bash
cp .env.production.example .env
```

Edit `.env` and set:
- `FUSION_API_KEY` — strong API key (required in production)
- `FUSION_CORS_ORIGINS` — comma-separated allowed origins
- `WARP_URL` — Warp API endpoint (the container reads it from the environment; nothing is passed on the command line)
- `FUSION_DUCKDB_EXTERNAL_ACCESS` — leave `false` unless you enable backups of the in-memory database (see below)

### 2. Build and Run

```bash
docker compose up -d
```

The image is built with `uv sync --frozen` from the committed `uv.lock` (Python 3.13), runs as a non-root user and starts `fusion-rest --auto-discover`.

Services:
- **fusion-rest** → `http://localhost:9000`
- **warp** → `http://localhost:8000`

### 3. Verify

```bash
curl http://localhost:9000/health
curl -H "X-API-Key: your-api-key" http://localhost:9000/readiness
open http://localhost:9000/docs
```

## Configuration

All settings are environment variables read once at startup by `Settings.from_env()`.

| Variable | Default | Description |
|----------|---------|-------------|
| `FUSION_ENV` | `development` | `production` enables fail-fast validation and hides debug endpoints |
| `FUSION_API_KEY` | *(required in production)* | API key for authentication |
| `FUSION_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `FUSION_RATE_LIMIT` | `100/minute` | Rate limit per API key (per IP without a key) |
| `FUSION_DEBUG_ENDPOINTS` | *(auto)* | `true`/`false` to force `/debug/config` on/off |
| `FUSION_LOG_LEVEL` | `info` | debug/info/warning/error |
| `FUSION_LOG_FORMAT` | `json` | json/text |
| `FUSION_LOG_FILE` | `/app/logs/fusion.log` | Optional log file (empty to disable) |
| `FUSION_DATABASE` | `:memory:` | DuckDB database (`:memory:` or a file path) |
| `FUSION_MEMORY_LIMIT` | `4GB` | DuckDB memory limit |
| `FUSION_THREADS` | `4` | DuckDB thread count |
| `FUSION_DUCKDB_EXTERNAL_ACCESS` | `false` | Allow DuckDB filesystem/network access (needed for export backups) |
| `FUSION_MAX_TEMP_DIRECTORY_SIZE` | *(unbounded)* | Cap on-disk spill, e.g. `10GB` |
| `FUSION_MAX_INGEST_ROWS` | `0` | Max rows pulled per table when loading (0 = unlimited) |
| `WARP_API_KEY` | *(none)* | API key sent to Warp on every request |
| `FUSION_WARP_API_KEY_HEADER` | `X-API-Key` | Header carrying it (Warp's `auth.header_name`) |
| `FUSION_FULL_LOAD_MAX_ROWS` | `500000` | Largest table loaded whole without a filter |
| `FUSION_SLICE_MAX_ROWS` | `500000` | Largest single slice (filtered read) |
| `FUSION_SLICE_BUDGET_ROWS` | `2000000` | Rows held across all slices before LRU eviction |
| `FUSION_SEMI_JOIN_MAX_KEYS` | `50000` | Most join keys passed to a source |
| `FUSION_IN_CHUNK_SIZE` | `1000` | Keys per request when passing them |
| `FUSION_REFRESH_CONFIG` | *(none)* | JSON: `{"src.table": {"watermark_column": "...", "key_columns": [...]}}` |
| `FUSION_CACHE_TTL` / `FUSION_CACHE_MAX_ENTRIES` | `300` / `500` | Query cache |
| `FUSION_BACKUP_ENABLED` | `false` | Enable automated backups |
| `FUSION_BACKUP_INTERVAL` / `FUSION_BACKUP_RETENTION_DAYS` | `3600` / `7` | Backup schedule and retention |
| `FUSION_BACKUP_PATH` | `/app/data/backups` | Backup directory |
| `FUSION_CIRCUIT_BREAKER_THRESHOLD` / `FUSION_CIRCUIT_BREAKER_TIMEOUT` | `5` / `60` | Circuit breaker |
| `FUSION_POOL_SIZE` / `FUSION_POOL_MAX_OVERFLOW` | `10` / `5` | HTTP connection pool |
| `WARP_URL` | `http://localhost:8000` | Warp API URL |
| `WARP_TIMEOUT` / `WARP_MAX_RETRIES` / `WARP_BACKOFF_FACTOR` | `30` / `3` / `2` | Warp HTTP client |

See `.env.production.example` for a complete template.

### Backups

- With the default in-memory database, a backup is an `EXPORT DATABASE` **directory** (`fusion_backup_<timestamp>/`). This requires `FUSION_DUCKDB_EXTERNAL_ACCESS=true`; otherwise `/backup/create` returns `409` and scheduled backups log an error.
- With a file database (`FUSION_DATABASE=/app/data/fusion.duckdb`), a backup is a checkpointed copy of the file (`fusion_backup_<timestamp>.duckdb`) and needs no external access.
- `/backup/list` reports each backup's `kind` (`export` or `file`), size and creation time. Restores are available through the SDK (`app.backup.restore_backup(path)`) and keep the security latch.

## API Usage

### Authentication

All requests except `/health`, `/readiness` and the OpenAPI docs require the key:

```bash
curl -H "X-API-Key: your-api-key" http://localhost:9000/sources
```

Missing key → `401`; wrong key → `403`.

### Rate Limiting

Default `100/minute` per API key (hashed) or, without a key, per client IP. Backup endpoints have stricter limits. Exceeding the limit returns `429`.

### Request Tracing

Every response carries `X-Request-ID`; send your own to correlate logs.

### Health Endpoints

```bash
GET /health      # liveness
GET /readiness   # 503 until a data source is connected
```

## Deployment Platforms

### Docker Compose (recommended)

```bash
docker compose up -d
docker compose --profile mcp up -d     # also start the MCP server container
```

### Bare Metal / VM

```bash
uv sync --extra rest --extra mcp       # or: pip install "fusion[rest,mcp]"
export FUSION_ENV=production
export FUSION_API_KEY=your-secret-key
export WARP_URL=http://warp:8000
fusion-rest --auto-discover
```

## Monitoring

Structured JSON log lines:

```json
{"timestamp": "2026-09-17T10:30:00+00:00", "level": "INFO",
 "logger": "fusion.adapters.inbound.rest.middleware.logging",
 "message": "{\"request_id\": \"abc123\", \"method\": \"GET\", \"path\": \"/sources\", \"status_code\": 200, \"duration_ms\": 45.23}"}
```

```bash
docker compose logs -f fusion-rest
```

Metrics: `GET /cache/stats`, `GET /backup/stats`, `GET /debug/config` (non-production).

## Scaling

Fusion is stateless (in-memory DuckDB). Run several instances behind a load balancer (sticky sessions help the cache); each connects to Warp independently. For vertical scaling raise the container limits and `FUSION_MEMORY_LIMIT` / `FUSION_THREADS` together.

## Security Best Practices

1. Rotate `FUSION_API_KEY` regularly
2. Terminate TLS in a reverse proxy (nginx, Traefik)
3. Never use `*` in `FUSION_CORS_ORIGINS`
4. Keep `FUSION_DUCKDB_EXTERNAL_ACCESS=false` unless you need export backups
5. Keep dependencies current: `uv lock --upgrade` and the CI `pip-audit` job

## Troubleshooting

**Circuit breaker tripped** — `Circuit breaker 'warp:mydb' is OPEN`: Warp is down or slow. Check `curl http://warp:8000/health`; the breaker retries after `FUSION_CIRCUIT_BREAKER_TIMEOUT` seconds.

**Rate limit exceeded** — `429`: raise `FUSION_RATE_LIMIT` or wait for the window.

**Out of memory** — raise `FUSION_MEMORY_LIMIT`, set `FUSION_MAX_TEMP_DIRECTORY_SIZE` to allow spilling, or cap ingest with `FUSION_MAX_INGEST_ROWS`. Lower `FUSION_FULL_LOAD_MAX_ROWS` and `FUSION_SLICE_BUDGET_ROWS` so big tables are read as slices instead of whole.

**"Refusing to load ..."** — the table is larger than `FUSION_FULL_LOAD_MAX_ROWS` and the query gives nothing to narrow it by. Add a WHERE condition on a column, select fewer columns, join it to a smaller table on an equality key, or raise the limit if the machine has the memory. Slicing needs Warp to do the filtering; against Warp >= 0.10 it also streams Arrow and reports table sizes without counting rows.

**Backup returns 409** — backups are disabled (`FUSION_BACKUP_ENABLED`) or the in-memory export needs `FUSION_DUCKDB_EXTERNAL_ACCESS=true`.

## Support

- **Documentation:** [README.md](README.md)
- **Issues:** GitHub Issues
