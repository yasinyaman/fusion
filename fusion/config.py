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
    
    @classmethod
    def requires_auth(cls) -> bool:
        """Check if API key authentication is required."""
        return bool(cls.API_KEY)


# Singleton instance
config = Config()
