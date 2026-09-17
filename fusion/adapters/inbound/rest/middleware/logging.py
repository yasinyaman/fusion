"""Structured request/response logging with ``X-Request-ID`` propagation."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

_MAX_REQUEST_ID_LEN = 128


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        request_id = _sanitize(request.headers.get("X-Request-ID")) or uuid.uuid4().hex

        response = await call_next(request)

        entry = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
        if response.status_code >= 500:
            logger.error(json.dumps(entry))
        elif response.status_code >= 400:
            logger.warning(json.dumps(entry))
        else:
            logger.info(json.dumps(entry))

        response.headers["X-Request-ID"] = request_id
        return response


def _sanitize(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_.")
    return cleaned[:_MAX_REQUEST_ID_LEN] or None
