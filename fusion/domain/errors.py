"""Fusion exception hierarchy."""


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
    """Raised when schema/catalog operations fail."""


class BackupError(FusionError):
    """Raised when a backup or restore operation cannot be performed."""


class CircuitOpenError(ConnectionError):
    """Raised when a circuit breaker refuses a call because the circuit is open."""
