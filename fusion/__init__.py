"""Fusion OLAP Engine - DuckDB-powered in-memory analytics with tool support."""

from fusion.backup import BackupManager
from fusion.cache import QueryCache
from fusion.catalog import SchemaCatalog
from fusion.config import config
from fusion.engine import OLAPEngine
from fusion.guardrails import SQLGuardrails
from fusion.result import QueryResult
from fusion.strategy import FetchPlan, FetchStrategy
from fusion.tools.definitions import get_mcp_tools, get_openai_tools
from fusion.tools.executor import ToolExecutor
from fusion.utils.circuit_breaker import CircuitBreaker
from fusion.utils.connection_pool import ConnectionPool
from fusion.utils.logger import setup_logging
from fusion.views.materialized import MaterializedViewManager

__version__ = "0.5.0"

__all__ = [
    "OLAPEngine",
    "QueryResult",
    "SQLGuardrails",
    "QueryCache",
    "SchemaCatalog",
    "MaterializedViewManager",
    "ToolExecutor",
    "FetchStrategy",
    "FetchPlan",
    "get_openai_tools",
    "get_mcp_tools",
    "config",
    "setup_logging",
    "BackupManager",
    "CircuitBreaker",
    "ConnectionPool",
]
