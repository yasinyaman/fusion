"""Runtime settings, read from the environment explicitly (never at import)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# API keys that must never be accepted in production (placeholders/examples).
PLACEHOLDER_API_KEYS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "your-api-key",
        "your-secure-api-key-here-change-in-production",
    }
)


def _bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """All tunables in one immutable object. Build with ``Settings.from_env()``."""

    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 9000

    # Warp gateway
    warp_url: str = "http://localhost:8000"
    warp_timeout: float = 30.0
    warp_max_retries: int = 3
    warp_backoff_factor: float = 2.0

    # Analytics store (DuckDB)
    database: str = ":memory:"
    memory_limit: str = "4GB"
    threads: int = 4
    # Allow DuckDB to touch the filesystem/network (read_csv, ATTACH, COPY,
    # EXPORT DATABASE, httpfs). Off by default; needed only for export backups.
    external_access: bool = False
    # Cap on-disk spill for queries exceeding memory_limit ("" = unbounded).
    max_temp_directory_size: str = ""
    # Max rows pulled from a source when materializing a table (0 = unlimited).
    max_ingest_rows: int = 0

    # Cache
    cache_ttl: int = 300
    cache_max_entries: int = 500

    # Security
    api_key: str = ""
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    rate_limit: str = "100/minute"
    # "" = auto (on outside production, off in production).
    debug_endpoints: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    log_file: str = "/app/logs/fusion.log"

    # Resilience (Warp HTTP)
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: float = 60.0
    pool_size: int = 10
    pool_max_overflow: int = 5

    # Backups
    backup_enabled: bool = False
    backup_interval: int = 3600
    backup_path: str = "/app/data/backups"
    backup_retention_days: int = 7

    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        get = env.get
        return cls(
            env=get("FUSION_ENV", "development"),
            host=get("FUSION_HOST", "0.0.0.0"),
            port=int(get("FUSION_PORT", "9000")),
            warp_url=get("WARP_URL", "http://localhost:8000"),
            warp_timeout=float(get("WARP_TIMEOUT", "30")),
            warp_max_retries=int(get("WARP_MAX_RETRIES", "3")),
            warp_backoff_factor=float(get("WARP_BACKOFF_FACTOR", "2")),
            database=get("FUSION_DATABASE", ":memory:"),
            memory_limit=get("FUSION_MEMORY_LIMIT", "4GB"),
            threads=int(get("FUSION_THREADS", "4")),
            external_access=_bool(get("FUSION_DUCKDB_EXTERNAL_ACCESS", "false")),
            max_temp_directory_size=get("FUSION_MAX_TEMP_DIRECTORY_SIZE", ""),
            max_ingest_rows=int(get("FUSION_MAX_INGEST_ROWS", "0")),
            cache_ttl=int(get("FUSION_CACHE_TTL", "300")),
            cache_max_entries=int(get("FUSION_CACHE_MAX_ENTRIES", "500")),
            api_key=get("FUSION_API_KEY", ""),
            cors_origins=_csv(get("FUSION_CORS_ORIGINS", "http://localhost:3000")),
            rate_limit=get("FUSION_RATE_LIMIT", "100/minute"),
            debug_endpoints=get("FUSION_DEBUG_ENDPOINTS", ""),
            log_level=get("FUSION_LOG_LEVEL", "info").upper(),
            log_format=get("FUSION_LOG_FORMAT", "json"),
            log_file=get("FUSION_LOG_FILE", "/app/logs/fusion.log"),
            circuit_breaker_threshold=int(get("FUSION_CIRCUIT_BREAKER_THRESHOLD", "5")),
            circuit_breaker_timeout=float(get("FUSION_CIRCUIT_BREAKER_TIMEOUT", "60")),
            pool_size=int(get("FUSION_POOL_SIZE", "10")),
            pool_max_overflow=int(get("FUSION_POOL_MAX_OVERFLOW", "5")),
            backup_enabled=_bool(get("FUSION_BACKUP_ENABLED", "false")),
            backup_interval=int(get("FUSION_BACKUP_INTERVAL", "3600")),
            backup_path=get("FUSION_BACKUP_PATH", "/app/data/backups"),
            backup_retention_days=int(get("FUSION_BACKUP_RETENTION_DAYS", "7")),
        )

    # -- derived flags ------------------------------------------------------

    def is_production(self) -> bool:
        return self.env == "production"

    def is_development(self) -> bool:
        return self.env == "development"

    def requires_auth(self) -> bool:
        return bool(self.api_key)

    def debug_endpoints_enabled(self) -> bool:
        """Admin/debug endpoints: off in production unless explicitly enabled."""
        if self.debug_endpoints:
            return self.debug_endpoints.lower() == "true"
        return not self.is_production()

    def warp_http_defaults(self) -> dict[str, Any]:
        """Resilience/timeout defaults merged under every Warp source config."""
        return {
            "timeout": self.warp_timeout,
            "max_retries": self.warp_max_retries,
            "backoff_factor": self.warp_backoff_factor,
            "pool_size": self.pool_size,
            "pool_max_overflow": self.pool_max_overflow,
            "circuit_breaker_threshold": self.circuit_breaker_threshold,
            "circuit_breaker_timeout": self.circuit_breaker_timeout,
        }

    def validate(self) -> list[str]:
        """Fatal misconfigurations for the current environment (empty = OK).

        Production must have a real API key (auth would otherwise be silently
        disabled) and must not allow every CORS origin.
        """
        errors: list[str] = []
        if self.is_production():
            if self.api_key.strip() in PLACEHOLDER_API_KEYS:
                errors.append(
                    "FUSION_API_KEY is empty or a placeholder in production. "
                    "Set a strong, unique API key (authentication would otherwise "
                    "be disabled, leaving the API open)."
                )
            if "*" in self.cors_origins:
                errors.append(
                    "FUSION_CORS_ORIGINS contains '*' in production. "
                    "Specify explicit allowed origins."
                )
        return errors
