"""Utility modules for Fusion."""

from fusion.utils.circuit_breaker import CircuitBreaker
from fusion.utils.connection_pool import ConnectionPool
from fusion.utils.logger import setup_logging

__all__ = ["CircuitBreaker", "ConnectionPool", "setup_logging"]
