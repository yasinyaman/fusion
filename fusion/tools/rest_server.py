"""FastAPI REST API Server for Fusion (Production-Ready).

Exposes all 10 analytics tools as HTTP endpoints with automatic
OpenAPI/Swagger documentation.

Features:
- API Key authentication
- Rate limiting
- CORS configuration
- Structured JSON logging
- Health/Readiness endpoints
- Graceful shutdown
- Circuit breaker
- Backup/restore endpoints

Run as CLI:
    fusion-rest --warp-url http://localhost:8080 --database mydb --port 9000

Swagger UI available at: http://localhost:9000/docs
"""

import argparse
import hashlib
import logging
import signal
import sys
from contextlib import asynccontextmanager
from typing import Any

from fusion.config import config
from fusion.utils.logger import setup_logging

# Setup structured logging
setup_logging()
logger = logging.getLogger(__name__)

# Module-level state (initialized in lifespan)
_executor = None
_engine = None
_backup_manager = None


def _get_executor():
    return _executor


def _rate_limit_key(request: Any) -> str:
    """Rate-limit per API key when present, else per client IP.

    Keying on the API key avoids both X-Forwarded-For spoofing and unfair
    throttling of many clients that share one NAT/proxy IP.
    """
    from slowapi.util import get_remote_address

    api_key = request.headers.get("X-API-Key")
    if api_key:
        # Hash so the raw secret isn't used as an in-memory bucket key.
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return get_remote_address(request)


def create_app(
    engine: Any = None,
    executor: Any = None,
    backup_manager: Any = None,
) -> Any:
    """Create the FastAPI application (Production-Ready).

    Can be called with pre-configured engine/executor (for testing)
    or will use module-level globals (for CLI).
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from fusion.exceptions import GuardrailViolation, QueryError, SchemaError
    from fusion.middleware.auth import AuthMiddleware
    from fusion.middleware.logging import StructuredLoggingMiddleware
    from fusion.tools.definitions import TOOL_DEFINITIONS

    # --- Pydantic Models ---
    class SQLRequest(BaseModel):
        sql: str

    class SearchRequest(BaseModel):
        table: str
        filter_column: str
        filter_value: str
        limit: int = 20

    class AggregateRequest(BaseModel):
        table: str
        group_by: str
        agg_column: str
        agg_func: str

    class CreateViewRequest(BaseModel):
        name: str
        sql: str
        refresh: str = "manual"

    class TableRequest(BaseModel):
        table: str

    class ViewNameRequest(BaseModel):
        name: str

    # --- Rate Limiter ---
    limiter = Limiter(key_func=_rate_limit_key, default_limits=[config.RATE_LIMIT])
    
    # --- Lifespan (startup/shutdown) ---
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Handle startup and shutdown events."""
        # Startup
        logger.info(f"Fusion REST API starting (env={config.ENV}, version=0.5.0)")
        
        # Start backup scheduler if enabled
        if _backup_manager and config.BACKUP_ENABLED:
            _backup_manager.start()
            logger.info("Backup scheduler started")
        
        yield
        
        # Shutdown
        logger.info("Fusion REST API shutting down...")
        
        # Stop backup scheduler
        if _backup_manager:
            _backup_manager.stop()
        
        # Close engine
        if _engine:
            _engine.close()
        
        logger.info("Shutdown complete")
    
    # --- App ---
    app = FastAPI(
        title="Fusion OLAP API",
        description=(
            "DuckDB-powered in-memory analytics engine. "
            "Query data sources via SQL with caching and materialized views."
        ),
        version="0.5.0",
        lifespan=lifespan,
    )
    
    # Add rate limiter to app state
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # --- Middleware Stack (order matters) ---
    # 1. Structured logging (outermost - logs everything)
    app.add_middleware(StructuredLoggingMiddleware)
    
    # 2. Authentication
    app.add_middleware(AuthMiddleware)
    
    # 3. CORS (configured from environment)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store executor/backup: injected (for testing) or module-level global (for CLI)
    _injected_executor = executor
    _injected_backup = backup_manager

    def _exec():
        """Get the executor — injected or global."""
        return _injected_executor if _injected_executor is not None else _get_executor()
    
    def _backup():
        """Get the backup manager — injected or global."""
        return _injected_backup if _injected_backup is not None else _backup_manager

    # --- Exception Handlers ---
    @app.exception_handler(GuardrailViolation)
    async def guardrail_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"error": str(exc)})

    @app.exception_handler(QueryError)
    async def query_error_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(SchemaError)
    async def schema_error_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": str(exc)})

    def _handle_result(result: dict):
        """Check tool result for errors and raise appropriate HTTP exceptions."""
        if "error" in result:
            error_msg = result["error"]
            if "Guardrail" in error_msg or "Only SELECT" in error_msg:
                raise HTTPException(status_code=403, detail=error_msg)
            elif "Unknown tool" in error_msg:
                raise HTTPException(status_code=404, detail=error_msg)
            else:
                raise HTTPException(status_code=400, detail=error_msg)
        return result

    # --- Health & Readiness ---
    @app.get("/health")
    def health():
        """Health check endpoint (always returns 200 if service is running)."""
        return {
            "status": "healthy",
            "version": "0.5.0",
            "environment": config.ENV,
        }
    
    @app.get("/readiness")
    def readiness():
        """Readiness check (validates connections and dependencies)."""
        try:
            # Check if executor is available
            if _exec() is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "reason": "Executor not initialized",
                    },
                )
            
            # Check if engine has connected sources
            sources = _exec().list_sources()
            if not sources.get("sources"):
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "reason": "No data sources connected",
                    },
                )
            
            # All checks passed
            return {
                "status": "ready",
                "version": "0.5.0",
                "sources": len(sources.get("sources", [])),
            }
        
        except Exception as e:
            logger.error(f"Readiness check failed: {e}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": str(e),
                },
            )
    
    @app.get("/tools")
    def list_tools():
        """List all available analytics tools."""
        return {"tools": TOOL_DEFINITIONS, "count": len(TOOL_DEFINITIONS)}

    # --- Generic Tool Dispatch ---
    @app.post("/tools/{tool_name}")
    def execute_tool(tool_name: str, arguments: dict = {}):
        """Execute any tool by name. Body is the arguments dict."""
        result = _exec().execute(tool_name, arguments)
        return _handle_result(result)

    # --- Convenience Endpoints ---
    @app.get("/sources")
    def get_sources():
        return _exec().list_sources()

    @app.get("/tables/{table:path}/schema")
    def get_table_schema(table: str):
        result = _exec().describe_table(table)
        return _handle_result(result)

    @app.post("/query")
    def run_query(req: SQLRequest):
        result = _exec().query_data(req.sql)
        return _handle_result(result)

    @app.post("/search")
    def search(req: SearchRequest):
        result = _exec().search_data(
            req.table, req.filter_column, req.filter_value, req.limit
        )
        return _handle_result(result)

    @app.post("/aggregate")
    def aggregate(req: AggregateRequest):
        result = _exec().aggregate_data(
            req.table, req.group_by, req.agg_column, req.agg_func
        )
        return _handle_result(result)

    @app.get("/views")
    def get_views():
        return _exec().list_views()

    @app.post("/views")
    def create_view(req: CreateViewRequest):
        result = _exec().create_view(req.name, req.sql, req.refresh)
        return _handle_result(result)

    @app.post("/views/{name}/refresh")
    def refresh_view(name: str):
        result = _exec().refresh_view(name)
        return _handle_result(result)

    @app.post("/tables/{table:path}/load")
    def load_table(table: str):
        result = _exec().load_table(table)
        return _handle_result(result)

    @app.get("/cache/stats")
    def cache_stats():
        return _exec().cache_stats()
    
    # --- Backup & Restore ---
    @app.get("/backup/list")
    @limiter.limit("10/minute")
    def list_backups(request: Request):
        """List all available backups."""
        if not _backup():
            return {"error": "Backup manager not initialized"}
        try:
            backups = _backup().list_backups()
            return {"backups": backups, "count": len(backups)}
        except Exception as e:
            logger.error(f"Failed to list backups: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/backup/create")
    @limiter.limit("5/minute")
    def create_backup(request: Request):
        """Create a new backup."""
        if not _backup():
            return {"error": "Backup manager not initialized"}
        try:
            backup_file = _backup().create_backup()
            return {
                "message": "Backup created successfully",
                "backup_file": str(backup_file),
            }
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.get("/backup/stats")
    def backup_stats():
        """Get backup manager statistics."""
        if not _backup():
            return {"error": "Backup manager not initialized"}
        return _backup().get_stats()
    
    # --- Admin/Debug ---
    @app.get("/debug/config")
    def debug_config():
        """Get current configuration (admin only, requires auth).

        Disabled in production by default (exposes internal topology); enable
        with FUSION_DEBUG_ENDPOINTS=true.
        """
        if not config.debug_endpoints_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        return {
            "environment": config.ENV,
            "warp_url": config.WARP_URL,
            "memory_limit": config.DUCKDB_MEMORY_LIMIT,
            "threads": config.DUCKDB_THREADS,
            "cache_ttl": config.CACHE_TTL,
            "cache_max_entries": config.CACHE_MAX_ENTRIES,
            "rate_limit": config.RATE_LIMIT,
            "cors_origins": config.CORS_ORIGINS,
            "backup_enabled": config.BACKUP_ENABLED,
            "auth_required": config.requires_auth(),
        }

    return app


def main():
    """CLI entry point for fusion-rest server (Production-Ready)."""
    parser = argparse.ArgumentParser(
        description="Fusion REST API Server — DuckDB analytics via HTTP (Production-Ready)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Server host (default: {config.HOST} from env)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Server port (default: {config.PORT} from env)",
    )
    parser.add_argument(
        "--warp-url",
        default=None,
        help=f"Warp REST API base URL (default: {config.WARP_URL} from env)",
    )
    parser.add_argument(
        "--database",
        default="primary_db",
        help="Database name to connect via Warp (default: primary_db)",
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        help=(
            "Auto-discover all databases from Warp and connect each as a "
            "separate source. When used, --database is ignored."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Additional source in 'name=X,url=Y,db=Z' format. "
            "Can be repeated for multi-source federation."
        ),
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    args = parser.parse_args()
    
    # Fail fast on fatal misconfiguration (e.g. production without an API key)
    config_errors = config.validate()
    if config_errors:
        for err in config_errors:
            logger.error("Configuration error: %s", err)
        logger.error("Refusing to start. Fix the above and retry.")
        sys.exit(1)

    # Use config values (can be overridden by CLI args)
    host = args.host or config.HOST
    port = args.port or config.PORT
    warp_url = args.warp_url or config.WARP_URL

    from fusion.backup import BackupManager
    from fusion.engine import OLAPEngine
    from fusion.tools.executor import ToolExecutor

    global _executor, _engine, _backup_manager

    # EXPORT DATABASE backups need DuckDB external access, which is off by default.
    if config.BACKUP_ENABLED and not config.DUCKDB_EXTERNAL_ACCESS:
        logger.warning(
            "Backups are enabled but FUSION_DUCKDB_EXTERNAL_ACCESS is false; "
            "EXPORT DATABASE will fail. Set FUSION_DUCKDB_EXTERNAL_ACCESS=true "
            "to allow backups (widens the attack surface)."
        )

    # Initialize engine
    _engine = OLAPEngine(
        database=config.DUCKDB_DATABASE,
        memory_limit=config.DUCKDB_MEMORY_LIMIT,
        threads=config.DUCKDB_THREADS,
        cache_max_entries=config.CACHE_MAX_ENTRIES,
        cache_ttl=config.CACHE_TTL,
        enable_external_access=config.DUCKDB_EXTERNAL_ACCESS,
        max_temp_directory_size=config.DUCKDB_MAX_TEMP_DIR_SIZE or None,
        max_ingest_rows=config.MAX_INGEST_ROWS,
    )

    # Connect Warp source(s)
    if args.auto_discover:
        from fusion.connectors.warp import WarpConnector

        databases = WarpConnector.discover_databases(warp_url)
        if not databases:
            logger.warning(
                "No databases discovered from %s, falling back to --database",
                warp_url,
            )
            databases = [args.database]
        for db_name in databases:
            _engine.connect_source(db_name, {
                "type": "warp",
                "base_url": warp_url,
                "database": db_name,
            })
        logger.info(
            "Auto-discovered %d databases from %s: %s",
            len(databases), warp_url, databases,
        )
    else:
        _engine.connect_source(args.database, {
            "type": "warp",
            "base_url": warp_url,
            "database": args.database,
        })

    # Connect additional sources
    for source_str in args.source:
        parts = dict(p.split("=", 1) for p in source_str.split(",") if "=" in p)
        src_name = parts.get("name", "")
        src_url = parts.get("url", "")
        src_db = parts.get("db", src_name)
        if src_name and src_url:
            _engine.connect_source(src_name, {
                "type": "warp",
                "base_url": src_url,
                "database": src_db,
            })

    _executor = ToolExecutor(_engine)
    
    # Initialize backup manager
    _backup_manager = BackupManager(_engine)

    # Graceful shutdown handler
    shutdown_event = False
    
    def signal_handler(signum, frame):
        nonlocal shutdown_event
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        shutdown_event = True
        
        # Stop backup manager
        if _backup_manager:
            _backup_manager.stop()
        
        # Close engine
        if _engine:
            _engine.close()
        
        logger.info("Graceful shutdown complete")
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info(
        "Starting Fusion REST API Server",
    )
    logger.info(f"  Environment: {config.ENV}")
    logger.info(f"  Host: {host}:{port}")
    logger.info(f"  Warp URL: {warp_url}")
    logger.info(f"  Database: {args.database}")
    logger.info(f"  Auth: {'enabled' if config.requires_auth() else 'disabled'}")
    logger.info(f"  Rate Limit: {config.RATE_LIMIT}")
    logger.info(f"  CORS: {config.CORS_ORIGINS}")
    logger.info(f"  Backup: {'enabled' if config.BACKUP_ENABLED else 'disabled'}")
    logger.info(f"  Docs: http://{host}:{port}/docs")

    import uvicorn

    app = create_app()
    
    # Run with graceful shutdown support
    uvicorn_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        reload=args.reload,
        log_config=None,  # Use our custom logging
    )
    server = uvicorn.Server(uvicorn_config)
    server.run()


if __name__ == "__main__":
    main()
