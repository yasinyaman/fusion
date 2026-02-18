"""API Key authentication middleware."""

from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from fusion.config import config


class AuthMiddleware(BaseHTTPMiddleware):
    """API Key authentication middleware.
    
    Validates X-API-Key header against configured API key.
    Skips authentication for health/docs endpoints.
    """

    EXCLUDED_PATHS = {"/health", "/readiness", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next: Callable):
        """Validate API key for protected endpoints."""
        # Skip auth if not configured
        if not config.requires_auth():
            return await call_next(request)
        
        # Skip auth for excluded paths
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)
        
        # Check API key header
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing X-API-Key header"},
            )
        
        if api_key != config.API_KEY:
            return JSONResponse(
                status_code=403,
                content={"error": "Invalid API key"},
            )
        
        return await call_next(request)
