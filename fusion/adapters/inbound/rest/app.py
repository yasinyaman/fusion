"""FastAPI application factory (no module-level state)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from fusion import __version__
from fusion.adapters.inbound.rest.middleware import AuthMiddleware, StructuredLoggingMiddleware
from fusion.adapters.inbound.rest.rate_limit import make_limiter
from fusion.adapters.inbound.rest.routes import register_routes
from fusion.application.app import FusionApp
from fusion.application.settings import Settings
from fusion.domain.errors import BackupError, GuardrailViolation, QueryError, SchemaError

logger = logging.getLogger(__name__)


def create_app(fusion: FusionApp, settings: Settings | None = None) -> FastAPI:
    """Build the REST API around an assembled ``FusionApp``.

    Middleware (outermost first): CORS, structured logging, API-key auth,
    rate limiting. The caller owns ``fusion`` and closes it on exit.
    """
    settings = settings or fusion.settings
    limiter = make_limiter(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Fusion REST API starting (env=%s, version=%s)", settings.env, __version__)
        if settings.backup_enabled:
            fusion.backup.start()
        yield
        fusion.backup.stop()
        logger.info("Fusion REST API shutting down")

    app = FastAPI(
        title="Fusion OLAP API",
        description=(
            "DuckDB-powered in-memory analytics engine. "
            "Query data sources via SQL with caching and materialized views."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.fusion = fusion
    app.state.settings = settings
    app.state.limiter = limiter

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # add_middleware() wraps outward: the last one added runs first.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(AuthMiddleware, api_key=settings.api_key)
    app.add_middleware(StructuredLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(GuardrailViolation)
    async def guardrail_handler(request: Request, exc: GuardrailViolation) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": str(exc)})

    @app.exception_handler(QueryError)
    async def query_error_handler(request: Request, exc: QueryError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(SchemaError)
    async def schema_error_handler(request: Request, exc: SchemaError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(BackupError)
    async def backup_error_handler(request: Request, exc: BackupError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc)})

    register_routes(app, limiter)
    return app
