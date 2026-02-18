"""Structured JSON logging middleware."""

import json
import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from fusion.config import config

logger = logging.getLogger(__name__)


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Log HTTP requests/responses in structured JSON format."""

    async def dispatch(self, request: Request, call_next: Callable):
        """Log request and response with timing."""
        start_time = time.time()
        
        # Extract request info
        request_id = request.headers.get("X-Request-ID", f"{time.time()}")
        
        # Call next middleware/endpoint
        response = await call_next(request)
        
        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000
        
        # Build structured log entry
        log_entry = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
        
        # Log based on status code
        if response.status_code >= 500:
            logger.error(json.dumps(log_entry))
        elif response.status_code >= 400:
            logger.warning(json.dumps(log_entry))
        else:
            logger.info(json.dumps(log_entry))
        
        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id
        
        return response
