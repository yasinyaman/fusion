"""Fusion custom exception classes."""


class FusionError(Exception):
    """Base exception for all Fusion errors."""


class ConnectionError(FusionError):
    """Raised when a data source connection fails."""


class GuardrailViolation(FusionError):
    """Raised when SQL fails guardrail validation."""


class QueryError(FusionError):
    """Raised when a SQL query execution fails."""


class CacheError(FusionError):
    """Raised when a cache operation fails."""


class SchemaError(FusionError):
    """Raised when schema operations fail."""


