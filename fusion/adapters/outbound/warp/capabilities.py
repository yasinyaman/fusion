"""What a running Warp can do, read from ``GET /info``.

Warp >= 0.10 publishes a ``capabilities`` block (typed schema endpoint,
streaming export formats, raw-query availability, URL layout). Older Warps
only expose ``settings.api_prefix`` / ``settings.raw_query_enabled``; for
them the source falls back to sampling and paged JSON, and probes whether a
single-database instance serves un-prefixed table routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_API_PREFIX = "/api/v1"


@dataclass(frozen=True, slots=True)
class WarpCapabilities:
    """Feature flags of one Warp instance (defaults describe a legacy 0.9)."""

    api_prefix: str = DEFAULT_API_PREFIX
    db_prefix: str = "unknown"  # "always" (>= 0.10) or "unknown" (legacy)
    schema: bool = False
    export_enabled: bool = False
    export_formats: tuple[str, ...] = ()
    export_max_rows: int = 0
    export_batch_size: int = 0
    raw_query: bool = True
    filter_ops: frozenset[str] = frozenset()
    version: str = ""

    @property
    def legacy(self) -> bool:
        """No capabilities block: a Warp older than 0.10."""
        return self.db_prefix == "unknown"

    @property
    def arrow(self) -> bool:
        return self.export_enabled and "arrow" in self.export_formats

    @property
    def export(self) -> bool:
        return self.export_enabled and bool(self.export_formats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "api_prefix": self.api_prefix,
            "db_prefix": self.db_prefix,
            "schema": self.schema,
            "export": self.export,
            "export_formats": list(self.export_formats),
            "export_max_rows": self.export_max_rows,
            "arrow": self.arrow,
            "raw_query": self.raw_query,
        }


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def from_info(info: Any) -> WarpCapabilities:
    """Parse a ``/info`` payload (any Warp version; tolerant of missing keys)."""
    payload = _dict(info)
    settings = _dict(payload.get("settings"))
    version = str(payload.get("version", "") or "")
    if not isinstance(payload.get("capabilities"), dict):
        return WarpCapabilities(
            api_prefix=str(settings.get("api_prefix") or DEFAULT_API_PREFIX),
            raw_query=bool(settings.get("raw_query_enabled", True)),
            version=version,
        )
    caps = _dict(payload.get("capabilities"))
    export = _dict(caps.get("export"))
    formats = _list(export.get("formats"))
    ops = _list(caps.get("filter_ops"))
    return WarpCapabilities(
        api_prefix=str(caps.get("api_prefix") or settings.get("api_prefix") or DEFAULT_API_PREFIX),
        db_prefix=str(caps.get("db_prefix") or "always"),
        schema=bool(caps.get("schema", False)),
        export_enabled=bool(export.get("enabled", False)),
        export_formats=tuple(str(f) for f in formats),
        export_max_rows=int(export.get("max_rows", 0) or 0),
        export_batch_size=int(export.get("batch_size", 0) or 0),
        raw_query=bool(caps.get("raw_query", settings.get("raw_query_enabled", False))),
        filter_ops=frozenset(str(op) for op in ops),
        version=version,
    )
