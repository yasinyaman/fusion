"""Fusion ports: the interfaces adapters implement and services depend on.

Ports are ``typing.Protocol`` classes expressed purely in domain types, so
any adapter (DuckDB, Warp, an in-memory fake, ...) can satisfy them without
the application layer knowing which one is plugged in.
"""

from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.data_source import (
    DatabaseDiscovery,
    DataSource,
    PushdownCapable,
    SourceFactory,
)
from fusion.ports.scheduler import ScheduledJob, Scheduler
from fusion.ports.sql_policy import SqlAnalyzer, SqlValidator

__all__ = [
    "AnalyticsStore",
    "DataSource",
    "DatabaseDiscovery",
    "PushdownCapable",
    "QueryCache",
    "ScheduledJob",
    "Scheduler",
    "SourceFactory",
    "SqlAnalyzer",
    "SqlValidator",
]
