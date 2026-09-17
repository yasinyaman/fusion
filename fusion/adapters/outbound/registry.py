"""Registry of data-source types -> factories (the SourceFactory port)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fusion.domain.errors import ConnectionError
from fusion.ports.data_source import DataSource, SourceFactory


class SourceRegistry:
    """Maps a ``config["type"]`` string to a factory building that source."""

    def __init__(self) -> None:
        self._factories: dict[str, SourceFactory] = {}

    def register(self, source_type: str, factory: SourceFactory) -> None:
        self._factories[source_type] = factory

    def types(self) -> list[str]:
        return sorted(self._factories)

    def create(self, name: str, config: Mapping[str, Any]) -> DataSource:
        source_type = config.get("type")
        if not source_type:
            raise ConnectionError("Config must include 'type' key")
        factory = self._factories.get(source_type)
        if factory is None:
            raise ConnectionError(
                f"Unknown source type '{source_type}'. Available: {', '.join(self.types())}"
            )
        return factory(name, config)


def default_registry(http_defaults: Mapping[str, Any] | None = None) -> SourceRegistry:
    """Registry with the built-in ``warp`` source.

    ``http_defaults`` (timeouts, retries, pool and circuit-breaker settings)
    are merged under each source's own config.
    """
    from fusion.adapters.outbound.warp.source import WarpSource

    defaults = dict(http_defaults or {})

    def build_warp(name: str, config: Mapping[str, Any]) -> DataSource:
        return WarpSource.from_config(name, {**defaults, **config})

    registry = SourceRegistry()
    registry.register("warp", build_warp)
    return registry
