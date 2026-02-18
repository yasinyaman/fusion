# Fusion OLAP Engine - Deployment Guide

Production-ready deployment guide for Fusion.

## Production Features

### ✅ Security
- **API Key Authentication** — X-API-Key header validation
- **CORS Configuration** — Whitelist domains (no wildcard)
- **SQL Guardrails** — AST-based SQL injection protection
- **Identifier Validation** — Regex validation for table/column names

### ✅ Reliability
- **Circuit Breaker** — Prevents cascading failures (Warp API)
- **Connection Pooling** — HTTP connection reuse with retry logic
- **Graceful Shutdown** — SIGTERM/SIGINT handling
- **Health Checks** — `/health` (liveness) and `/readiness` endpoints

### ✅ Performance
- **Rate Limiting** — Per-IP request throttling (slowapi)
- **Query Cache** — LRU cache with TTL
- **Lazy Loading** — On-demand table fetching
- **Query Pushdown** — SQL delegation to source databases

### ✅ Observability
- **Structured Logging** — JSON format for production
- **Request Tracing** — X-Request-ID header tracking
- **Metrics Endpoints** — Cache stats, backup stats, config debug

### ✅ Data Management
- **Automated Backups** — Periodic DuckDB snapshots
- **Retention Policy** — Configurable backup cleanup
- **Backup API** — Create/list/restore via REST

## Quick Start (Docker)

### 1. Configure Environment

Copy and edit production config:

```bash
cp .env.production .env
```

Edit `.env` and set:
- `FUSION_API_KEY` — Strong API key (required)
- `FUSION_CORS_ORIGINS` — Comma-separated allowed origins
- `WARP_URL` — Warp API endpoint

### 2. Build and Run

```bash
docker-compose up -d
```

Services:
- **fusion-rest** → `http://localhost:9000`
- **warp** → `http://localhost:8000`

### 3. Verify Deployment

```bash
# Health check
curl http://localhost:9000/health

# Readiness check (with API key)
curl -H "X-API-Key: your-api-key" http://localhost:9000/readiness

# Swagger UI
open http://localhost:9000/docs
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FUSION_ENV` | `production` | Environment (production/development) |
| `FUSION_API_KEY` | *(required)* | API key for authentication |
| `FUSION_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `FUSION_RATE_LIMIT` | `100/minute` | Rate limit per IP |
| `FUSION_LOG_LEVEL` | `info` | Log level (debug/info/warning/error) |
| `FUSION_LOG_FORMAT` | `json` | Log format (json/text) |
| `FUSION_MEMORY_LIMIT` | `4GB` | DuckDB memory limit |
| `FUSION_THREADS` | `4` | DuckDB thread count |
| `FUSION_CACHE_TTL` | `300` | Cache TTL in seconds |
| `FUSION_BACKUP_ENABLED` | `true` | Enable automated backups |
| `FUSION_BACKUP_INTERVAL` | `3600` | Backup interval in seconds |
| `WARP_URL` | `http://warp:8000` | Warp API URL |
| `WARP_TIMEOUT` | `30` | Warp request timeout (seconds) |
| `WARP_MAX_RETRIES` | `3` | Max retry attempts |

See `.env.production` for full list.

## API Usage

### Authentication

All requests (except `/health`, `/docs`) require API key:

```bash
curl -H "X-API-Key: your-api-key" \
  http://localhost:9000/sources
```

### Rate Limiting

Default: 100 requests/minute per IP.

Response headers:
- `X-RateLimit-Limit` — Max requests per window
- `X-RateLimit-Remaining` — Remaining requests
- `X-RateLimit-Reset` — Reset timestamp

### Request Tracing

All responses include `X-Request-ID` header for tracking.

### Health Endpoints

```bash
# Liveness (always returns 200 if running)
GET /health

# Readiness (validates connections)
GET /readiness
```

### Backup Endpoints

```bash
# List backups
GET /backup/list

# Create backup
POST /backup/create

# Backup stats
GET /backup/stats
```

## Deployment Platforms

### Docker Compose (Recommended)

```bash
docker-compose up -d
```

### Kubernetes

Example manifests in `k8s/` (coming soon).

### Bare Metal / VM

```bash
# Install dependencies
pip install -e ".[all]"

# Set environment variables
export FUSION_ENV=production
export FUSION_API_KEY=your-secret-key
export WARP_URL=http://warp:8000

# Run server
fusion-rest --warp-url $WARP_URL --auto-discover
```

## Monitoring

### Logs

Structured JSON logs:

```json
{
  "timestamp": "2025-02-18T10:30:00Z",
  "level": "INFO",
  "logger": "fusion.tools.rest_server",
  "message": "HTTP GET /sources -> 200 (45.23ms)",
  "request_id": "abc123",
  "duration_ms": 45.23
}
```

View logs:

```bash
docker-compose logs -f fusion-rest
```

### Metrics

```bash
# Cache statistics
GET /cache/stats

# Backup statistics
GET /backup/stats

# Configuration (debug)
GET /debug/config
```

## Scaling

### Horizontal Scaling

Fusion is stateless (in-memory DuckDB). To scale:

1. Deploy multiple instances behind a load balancer
2. Use sticky sessions for query caching efficiency
3. Each instance connects independently to Warp

### Vertical Scaling

Increase resources in `docker-compose.yml`:

```yaml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 16G
```

Update `FUSION_MEMORY_LIMIT` and `FUSION_THREADS` accordingly.

## Security Best Practices

1. **Rotate API Keys** — Change `FUSION_API_KEY` regularly
2. **Use HTTPS** — Deploy behind reverse proxy (nginx, Traefik)
3. **Restrict CORS** — Never use `*` in production
4. **Network Isolation** — Use Docker networks, firewall rules
5. **Keep Updated** — Regularly update dependencies

## Troubleshooting

### Circuit Breaker Tripped

```json
{"error": "Circuit breaker 'warp_mydb' is OPEN"}
```

**Cause:** Warp API is down or slow.

**Fix:**
1. Check Warp health: `curl http://warp:8000/health`
2. Reset circuit: Restart Fusion or wait for timeout

### Rate Limit Exceeded

```json
{"error": "Rate limit exceeded"}
```

**Fix:** Increase `FUSION_RATE_LIMIT` or wait for window reset.

### Out of Memory

```
DuckDB: Out of Memory Error
```

**Fix:** Increase `FUSION_MEMORY_LIMIT` or optimize queries.

## Support

- **Documentation:** [README.md](README.md)
- **Architecture:** [CLAUDE.md](CLAUDE.md)
- **Issues:** GitHub Issues (if open source)
