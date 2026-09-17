"""Starlette middleware for the REST adapter."""

from fusion.adapters.inbound.rest.middleware.auth import AuthMiddleware
from fusion.adapters.inbound.rest.middleware.logging import StructuredLoggingMiddleware

__all__ = ["AuthMiddleware", "StructuredLoggingMiddleware"]
