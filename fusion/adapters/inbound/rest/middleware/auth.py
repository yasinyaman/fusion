"""API-key authentication middleware (``X-API-Key`` header)."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

EXCLUDED_PATHS = frozenset({"/health", "/readiness", "/docs", "/openapi.json", "/redoc"})


class AuthMiddleware(BaseHTTPMiddleware):
    """Rejects requests without the configured API key.

    Disabled when ``api_key`` is empty (development). Health, readiness and
    the OpenAPI docs stay open.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str = "",
        on_failure: Callable[[Request], bool] | None = None,
    ) -> None:
        super().__init__(app)
        self._api_key = api_key
        #: Called on every rejected key; True means "too many, answer 429".
        self._on_failure = on_failure

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._api_key or request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        provided = request.headers.get("X-API-Key")
        # Constant-time comparison: no timing side channel on the key.
        if provided and hmac.compare_digest(provided.encode(), self._api_key.encode()):
            return await call_next(request)
        if self._on_failure is not None and self._on_failure(request):
            return JSONResponse(status_code=429, content={"error": "Too many failed API keys"})
        if not provided:
            return JSONResponse(status_code=401, content={"error": "Missing X-API-Key header"})
        return JSONResponse(status_code=403, content={"error": "Invalid API key"})
