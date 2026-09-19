"""Runtime settings, read from the environment explicitly (never at import)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fusion.domain.errors import SchemaError
from fusion.domain.measures import (
    ROW_MEASURE,
    Dimension,
    Measure,
    SemanticModel,
    normalize_agg,
)
from fusion.domain.models import RefreshSpec, coerce_ref
from fusion.domain.policy import MaterializationPolicy

logger = logging.getLogger(__name__)

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
    # Sent on every Warp request in ``warp_api_key_header`` (Warp's
    # ``auth.header_name``, ``X-API-Key`` by default). Empty = no auth.
    warp_api_key: str = ""
    warp_api_key_header: str = "X-API-Key"
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

    # How much data a query may pull in. A table bigger than
    # ``full_load_max_rows`` is only read through a slice (the query's own
    # WHERE and column list); if that is still too big the query is refused
    # with concrete advice instead of filling memory.
    full_load_max_rows: int = 500_000
    slice_max_rows: int = 500_000
    slice_budget_rows: int = 2_000_000
    semi_join_max_keys: int = 50_000
    in_chunk_size: int = 1_000

    # How each table is refreshed, as JSON:
    # {"ecommerce.orders": {"watermark_column": "updated_at",
    #                       "key_columns": ["id"]}}
    # A table listed here is refreshed incrementally (only rows above the
    # highest watermark already loaded); everything else is re-fetched whole.
    refresh_config: str = ""
    # JSON: {"source.table": {"measures": [...], "dimensions": [...]}}
    semantic_model: str = ""

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
            warp_api_key=get("WARP_API_KEY", ""),
            warp_api_key_header=get("FUSION_WARP_API_KEY_HEADER", "X-API-Key").strip()
            or "X-API-Key",
            warp_timeout=float(get("WARP_TIMEOUT", "30")),
            warp_max_retries=int(get("WARP_MAX_RETRIES", "3")),
            warp_backoff_factor=float(get("WARP_BACKOFF_FACTOR", "2")),
            database=get("FUSION_DATABASE", ":memory:"),
            memory_limit=get("FUSION_MEMORY_LIMIT", "4GB"),
            threads=int(get("FUSION_THREADS", "4")),
            external_access=_bool(get("FUSION_DUCKDB_EXTERNAL_ACCESS", "false")),
            max_temp_directory_size=get("FUSION_MAX_TEMP_DIRECTORY_SIZE", ""),
            max_ingest_rows=int(get("FUSION_MAX_INGEST_ROWS", "0")),
            full_load_max_rows=int(get("FUSION_FULL_LOAD_MAX_ROWS", "500000")),
            slice_max_rows=int(get("FUSION_SLICE_MAX_ROWS", "500000")),
            slice_budget_rows=int(get("FUSION_SLICE_BUDGET_ROWS", "2000000")),
            semi_join_max_keys=int(get("FUSION_SEMI_JOIN_MAX_KEYS", "50000")),
            in_chunk_size=int(get("FUSION_IN_CHUNK_SIZE", "1000")),
            refresh_config=get("FUSION_REFRESH_CONFIG", ""),
            semantic_model=get("FUSION_SEMANTIC_MODEL", ""),
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

    def policy(self) -> MaterializationPolicy:
        """The row budgets the planner works to."""
        return MaterializationPolicy(
            full_load_max_rows=self.full_load_max_rows,
            slice_max_rows=self.slice_max_rows,
            slice_budget_rows=self.slice_budget_rows,
            semi_join_max_keys=self.semi_join_max_keys,
            in_chunk_size=self.in_chunk_size,
        )

    def refresh_specs(self) -> dict[str, RefreshSpec]:
        """Per-table incremental refresh settings, keyed by ``source.table``.

        A malformed value is logged and ignored: a bad refresh hint must not
        stop the engine from starting.
        """
        if not self.refresh_config.strip():
            return {}
        try:
            parsed = json.loads(self.refresh_config)
        except ValueError as e:
            logger.error("FUSION_REFRESH_CONFIG is not valid JSON, ignoring it: %s", e)
            return {}
        if not isinstance(parsed, dict):
            logger.error("FUSION_REFRESH_CONFIG must be a JSON object, ignoring it")
            return {}
        specs: dict[str, RefreshSpec] = {}
        for name, raw in parsed.items():
            spec = refresh_spec_from(raw)
            if spec is None:
                logger.error("Ignoring refresh config for '%s': expected an object", name)
                continue
            specs[str(name)] = spec
        return specs

    def semantic_models(self) -> dict[str, SemanticModel]:
        """Explicit semantic models, keyed by ``source.table``.

        Same posture as :meth:`refresh_specs`: a malformed value is logged and
        ignored rather than fatal, because a table with no configured model
        still gets an inferred one and stays queryable.
        """
        if not self.semantic_model.strip():
            return {}
        try:
            parsed = json.loads(self.semantic_model)
        except ValueError as e:
            logger.error("FUSION_SEMANTIC_MODEL is not valid JSON, ignoring it: %s", e)
            return {}
        if not isinstance(parsed, dict):
            logger.error("FUSION_SEMANTIC_MODEL must be a JSON object, ignoring it")
            return {}
        models: dict[str, SemanticModel] = {}
        for name, raw in parsed.items():
            try:
                models[str(name)] = semantic_model_from(str(name), raw)
            except (SchemaError, TypeError, ValueError) as e:
                logger.error("Ignoring semantic model for '%s': %s", name, e)
        return models

    def warp_http_defaults(self) -> dict[str, Any]:
        """Auth/resilience/timeout defaults merged under every Warp source config."""
        return {
            "api_key": self.warp_api_key or None,
            "api_key_header": self.warp_api_key_header,
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


def refresh_spec_from(raw: Any) -> RefreshSpec | None:
    """Build a RefreshSpec from a config mapping (None when it is not one)."""
    if not isinstance(raw, Mapping):
        return None
    keys = raw.get("key_columns") or ()
    if isinstance(keys, str):
        keys = [keys]
    return RefreshSpec(
        watermark_column=str(raw.get("watermark_column", "") or ""),
        key_columns=tuple(str(k) for k in keys),
    )


def semantic_model_from(table: str, raw: Any) -> SemanticModel:
    """Build a SemanticModel from a config mapping.

    A measure is ``{"name", "column", "agg"?, "numeric"?, "description"?}`` and
    a dimension ``{"name", "column", "temporal"?, "description"?}``; a bare
    string is shorthand for a column of the same name. The row measure is added
    automatically, so ``*:count`` works against a configured model too.

    Raises:
        ValueError: On a malformed entry, so the caller can log and skip it.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("expected an object")
    ref = coerce_ref(table)
    measures = [Measure(name=ROW_MEASURE, column=ROW_MEASURE, default_agg="COUNT")]
    for entry in raw.get("measures") or ():
        spec = {"name": entry, "column": entry} if isinstance(entry, str) else entry
        if not isinstance(spec, Mapping) or not spec.get("name"):
            raise ValueError(f"malformed measure {entry!r}")
        column = str(spec.get("column") or spec["name"])
        measures.append(
            Measure(
                name=str(spec["name"]),
                column=column,
                default_agg=normalize_agg(str(spec.get("agg", "SUM")), str(spec["name"])),
                numeric=bool(spec.get("numeric", True)),
                description=str(spec.get("description", "")),
            )
        )
    dimensions = []
    for entry in raw.get("dimensions") or ():
        spec = {"name": entry, "column": entry} if isinstance(entry, str) else entry
        if not isinstance(spec, Mapping) or not spec.get("name"):
            raise ValueError(f"malformed dimension {entry!r}")
        dimensions.append(
            Dimension(
                name=str(spec["name"]),
                column=str(spec.get("column") or spec["name"]),
                temporal=bool(spec.get("temporal", False)),
                description=str(spec.get("description", "")),
            )
        )
    return SemanticModel(
        ref=ref, measures=tuple(measures), dimensions=tuple(dimensions), source="configured"
    )
