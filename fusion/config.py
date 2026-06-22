"""Configuration management with environment variable support."""

import os
from typing import List


class Config:
    """Application configuration loaded from environment variables."""

    # Environment
    ENV: str = os.getenv("FUSION_ENV", "development")
    
    # Server
    HOST: str = os.getenv("FUSION_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("FUSION_PORT", "9000"))
    
    # Warp API
    WARP_URL: str = os.getenv("WARP_URL", "http://localhost:8000")
    WARP_TIMEOUT: int = int(os.getenv("WARP_TIMEOUT", "30"))
    WARP_MAX_RETRIES: int = int(os.getenv("WARP_MAX_RETRIES", "3"))
    WARP_BACKOFF_FACTOR: float = float(os.getenv("WARP_BACKOFF_FACTOR", "2"))
    
    # DuckDB
    DUCKDB_DATABASE: str = os.getenv("FUSION_DATABASE", ":memory:")
    DUCKDB_MEMORY_LIMIT: str = os.getenv("FUSION_MEMORY_LIMIT", "4GB")
    DUCKDB_THREADS: int = int(os.getenv("FUSION_THREADS", "4"))
    # Allow DuckDB to touch the local filesystem/network (read_csv, ATTACH,
    # COPY, EXPORT DATABASE, httpfs). Off by default for security. Turn on only
    # if you rely on EXPORT DATABASE backups and accept the larger attack surface.
    DUCKDB_EXTERNAL_ACCESS: bool = (
        os.getenv("FUSION_DUCKDB_EXTERNAL_ACCESS", "false").lower() == "true"
    )
    # Cap on-disk spill for queries that exceed memory_limit (e.g. heavy
    # federated joins). Empty = use DuckDB's default (unbounded).
    DUCKDB_MAX_TEMP_DIR_SIZE: str = os.getenv("FUSION_MAX_TEMP_DIRECTORY_SIZE", "")
    # Max rows pulled from a source when materializing a table in DuckDB.
    # 0 = unlimited. Guards against OOM on the non-pushdown fallback path.
    MAX_INGEST_ROWS: int = int(os.getenv("FUSION_MAX_INGEST_ROWS", "0"))
    
    # Cache
    CACHE_TTL: int = int(os.getenv("FUSION_CACHE_TTL", "300"))
    CACHE_MAX_ENTRIES: int = int(os.getenv("FUSION_CACHE_MAX_ENTRIES", "500"))
    
    # Security
    API_KEY: str = os.getenv("FUSION_API_KEY", "")
    CORS_ORIGINS: List[str] = os.getenv(
        "FUSION_CORS_ORIGINS", 
        "http://localhost:3000"
    ).split(",")
    ALLOWED_HOSTS: List[str] = os.getenv(
        "FUSION_ALLOWED_HOSTS",
        "localhost"
    ).split(",")
    
    # Rate Limiting
    RATE_LIMIT: str = os.getenv("FUSION_RATE_LIMIT", "100/minute")
    RATE_LIMIT_BURST: int = int(os.getenv("FUSION_RATE_LIMIT_BURST", "20"))
    
    # Logging
    LOG_LEVEL: str = os.getenv("FUSION_LOG_LEVEL", "info").upper()
    LOG_FORMAT: str = os.getenv("FUSION_LOG_FORMAT", "json")
    LOG_FILE: str = os.getenv("FUSION_LOG_FILE", "/app/logs/fusion.log")
    
    # Circuit Breaker
    CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("FUSION_CIRCUIT_BREAKER_THRESHOLD", "5"))
    CIRCUIT_BREAKER_TIMEOUT: int = int(os.getenv("FUSION_CIRCUIT_BREAKER_TIMEOUT", "60"))
    
    # Connection Pool
    POOL_SIZE: int = int(os.getenv("FUSION_POOL_SIZE", "10"))
    POOL_MAX_OVERFLOW: int = int(os.getenv("FUSION_POOL_MAX_OVERFLOW", "5"))
    
    # Backup
    BACKUP_ENABLED: bool = os.getenv("FUSION_BACKUP_ENABLED", "false").lower() == "true"
    BACKUP_INTERVAL: int = int(os.getenv("FUSION_BACKUP_INTERVAL", "3600"))
    BACKUP_PATH: str = os.getenv("FUSION_BACKUP_PATH", "/app/data/backups")
    BACKUP_RETENTION_DAYS: int = int(os.getenv("FUSION_BACKUP_RETENTION_DAYS", "7"))

    @classmethod
    def is_production(cls) -> bool:
        """Check if running in production environment."""
        return cls.ENV == "production"
    
    @classmethod
    def is_development(cls) -> bool:
        """Check if running in development environment."""
        return cls.ENV == "development"
    
    # Debug endpoints (e.g. /debug/config) expose internal topology (warp_url,
    # CORS, ...). "" = auto (on outside production, off in production).
    DEBUG_ENDPOINTS: str = os.getenv("FUSION_DEBUG_ENDPOINTS", "")

    @classmethod
    def requires_auth(cls) -> bool:
        """Check if API key authentication is required."""
        return bool(cls.API_KEY)

    @classmethod
    def debug_endpoints_enabled(cls) -> bool:
        """Whether admin/debug endpoints should be served.

        Off in production unless FUSION_DEBUG_ENDPOINTS is explicitly set true.
        """
        if cls.DEBUG_ENDPOINTS:
            return cls.DEBUG_ENDPOINTS.lower() == "true"
        return not cls.is_production()

    # API keys that must never be accepted in production (placeholders/examples)
    PLACEHOLDER_API_KEYS = frozenset({
        "",
        "changeme",
        "change-me",
        "your-api-key",
        "your-secure-api-key-here-change-in-production",
    })

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of fatal misconfigurations for the current environment.

        Production must have a real API key and must not silently run with
        authentication disabled. Returns an empty list when configuration is OK.
        """
        errors: list[str] = []
        if cls.is_production():
            if cls.API_KEY.strip() in cls.PLACEHOLDER_API_KEYS:
                errors.append(
                    "FUSION_API_KEY is empty or a placeholder in production. "
                    "Set a strong, unique API key (authentication would otherwise "
                    "be disabled, leaving the API open)."
                )
            if "*" in cls.CORS_ORIGINS:
                errors.append(
                    "FUSION_CORS_ORIGINS contains '*' in production. "
                    "Specify explicit allowed origins."
                )
        return errors


# Singleton instance
config = Config()
