"""Fusion — DuckDB-powered in-memory OLAP engine with LLM tool support.

Quick start::

    from fusion import Settings, build_app

    app = build_app(Settings(memory_limit="4GB"))
    app.sources.connect("mydb", {"type": "warp", "base_url": "http://localhost:8000"})
    app.tools.query_data("SELECT * FROM mydb.orders LIMIT 10")
    app.close()

Importing ``fusion`` is cheap: only the pure domain and application layers
load eagerly; adapters (DuckDB, sqlglot, requests, ...) are imported when
``build_app`` is first called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fusion import ports
from fusion.application.app import FusionApp
from fusion.application.settings import Settings
from fusion.application.tool_schemas import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    get_mcp_tools,
    get_openai_tools,
)
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import (
    BackupError,
    CacheError,
    ConnectionError,
    FusionError,
    GuardrailViolation,
    QueryError,
    SchemaError,
)
from fusion.domain.models import (
    BackupInfo,
    ColumnInfo,
    FetchPlan,
    QueryResult,
    RowSet,
    TableRef,
    TableSchema,
)

if TYPE_CHECKING:
    from fusion.bootstrap import build_app

__version__ = "0.5.0"

__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "BackupError",
    "BackupInfo",
    "CacheError",
    "ColumnInfo",
    "ConnectionError",
    "FetchPlan",
    "FusionApp",
    "FusionError",
    "GuardrailViolation",
    "QueryError",
    "QueryResult",
    "RowSet",
    "SchemaCatalog",
    "SchemaError",
    "Settings",
    "TableRef",
    "TableSchema",
    "__version__",
    "build_app",
    "get_mcp_tools",
    "get_openai_tools",
    "ports",
]


def __getattr__(name: str) -> Any:
    # Lazy so `import fusion` never pulls in DuckDB/sqlglot/requests.
    if name == "build_app":
        from fusion.bootstrap import build_app

        return build_app
    raise AttributeError(f"module 'fusion' has no attribute {name!r}")
