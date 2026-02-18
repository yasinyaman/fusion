"""Fusion data source connectors with factory pattern."""

import importlib

from fusion.connectors.base import BaseConnector
from fusion.exceptions import ConnectionError

CONNECTOR_REGISTRY: dict[str, str] = {
    "warp": "fusion.connectors.warp.WarpConnector",
}


def create_connector(name: str, config: dict) -> BaseConnector:
    """Factory function: creates a connector based on config['type']."""
    source_type = config.get("type")
    if not source_type:
        raise ConnectionError("Config must include 'type' key")

    class_path = CONNECTOR_REGISTRY.get(source_type)
    if not class_path:
        raise ConnectionError(
            f"Unknown source type '{source_type}'. "
            f"Available: {', '.join(CONNECTOR_REGISTRY.keys())}"
        )

    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    connector_class = getattr(module, class_name)
    return connector_class(name, config)
