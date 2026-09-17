"""REST routes: the 10 tools plus health, readiness, backup and debug endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter

from fusion import __version__
from fusion.adapters.inbound.rest.schemas import (
    AggregateRequest,
    CreateViewRequest,
    SearchRequest,
    SQLRequest,
)
from fusion.application.app import FusionApp
from fusion.application.settings import Settings
from fusion.application.tool_schemas import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

_FORBIDDEN_MARKERS = ("Guardrail", "Only SELECT", "Blocked", "Multi-statement")


def fusion_of(request: Request) -> FusionApp:
    app: FusionApp = request.app.state.fusion
    return app


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def handle_result(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a tool ``{"error": ...}`` dict into the matching HTTP error."""
    if "error" in result:
        message = str(result["error"])
        if any(marker in message for marker in _FORBIDDEN_MARKERS):
            raise HTTPException(status_code=403, detail=message)
        if "Unknown tool" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)
    return result


def register_routes(app: FastAPI, limiter: Limiter) -> None:
    # -- health -------------------------------------------------------------

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """Liveness: 200 whenever the process serves requests."""
        return {
            "status": "healthy",
            "version": __version__,
            "environment": settings_of(request).env,
        }

    @app.get("/readiness")
    def readiness(request: Request) -> Any:
        """Readiness: at least one data source must be connected."""
        try:
            sources = fusion_of(request).tools.list_sources()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Readiness check failed: %s", e)
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(e)})
        if not sources.get("sources"):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "No data sources connected"},
            )
        return {"status": "ready", "version": __version__, "sources": len(sources["sources"])}

    # -- tools --------------------------------------------------------------

    @app.get("/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": TOOL_DEFINITIONS, "count": len(TOOL_DEFINITIONS)}

    @app.post("/tools/{tool_name}")
    def execute_tool(
        request: Request, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Generic dispatch: the body is the tool's arguments dict."""
        return handle_result(fusion_of(request).tools.execute(tool_name, arguments or {}))

    @app.get("/sources")
    def get_sources(request: Request) -> dict[str, Any]:
        return fusion_of(request).tools.list_sources()

    @app.get("/tables/{table:path}/schema")
    def get_table_schema(request: Request, table: str) -> dict[str, Any]:
        return handle_result(fusion_of(request).tools.describe_table(table))

    @app.post("/query")
    def run_query(request: Request, body: SQLRequest) -> dict[str, Any]:
        return handle_result(fusion_of(request).tools.query_data(body.sql))

    @app.post("/search")
    def search(request: Request, body: SearchRequest) -> dict[str, Any]:
        tools = fusion_of(request).tools
        return handle_result(
            tools.search_data(body.table, body.filter_column, body.filter_value, body.limit)
        )

    @app.post("/aggregate")
    def aggregate(request: Request, body: AggregateRequest) -> dict[str, Any]:
        tools = fusion_of(request).tools
        return handle_result(
            tools.aggregate_data(body.table, body.group_by, body.agg_column, body.agg_func)
        )

    @app.get("/views")
    def get_views(request: Request) -> dict[str, Any]:
        return fusion_of(request).tools.list_views()

    @app.post("/views")
    def create_view(request: Request, body: CreateViewRequest) -> dict[str, Any]:
        return handle_result(
            fusion_of(request).tools.create_view(body.name, body.sql, body.refresh)
        )

    @app.post("/views/{name}/refresh")
    def refresh_view(request: Request, name: str) -> dict[str, Any]:
        return handle_result(fusion_of(request).tools.refresh_view(name))

    @app.post("/tables/{table:path}/load")
    def load_table(request: Request, table: str) -> dict[str, Any]:
        return handle_result(fusion_of(request).tools.load_table(table))

    @app.get("/cache/stats")
    def cache_stats(request: Request) -> dict[str, Any]:
        return fusion_of(request).tools.cache_stats()

    # -- backup -------------------------------------------------------------

    @app.get("/backup/list")
    @limiter.limit("10/minute")
    def list_backups(request: Request) -> dict[str, Any]:
        backups = [b.as_dict() for b in fusion_of(request).backup.list_backups()]
        return {"backups": backups, "count": len(backups)}

    @app.post("/backup/create")
    @limiter.limit("5/minute")
    def create_backup(request: Request) -> dict[str, Any]:
        info = fusion_of(request).backup.create_backup()
        return {"message": "Backup created successfully", "backup": info.as_dict()}

    @app.get("/backup/stats")
    def backup_stats(request: Request) -> dict[str, Any]:
        return fusion_of(request).backup.get_stats()

    # -- debug --------------------------------------------------------------

    @app.get("/debug/config")
    def debug_config(request: Request) -> dict[str, Any]:
        """Effective configuration (hidden in production unless enabled)."""
        settings = settings_of(request)
        if not settings.debug_endpoints_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        return {
            "environment": settings.env,
            "warp_url": settings.warp_url,
            "memory_limit": settings.memory_limit,
            "threads": settings.threads,
            "cache_ttl": settings.cache_ttl,
            "cache_max_entries": settings.cache_max_entries,
            "rate_limit": settings.rate_limit,
            "cors_origins": list(settings.cors_origins),
            "backup_enabled": settings.backup_enabled,
            "auth_required": settings.requires_auth(),
        }
