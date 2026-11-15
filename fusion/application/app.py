"""FusionApp: the assembled application (services + the ports they use)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fusion.application.backup import BackupService
from fusion.application.query import QueryService
from fusion.application.semantic import SemanticService
from fusion.application.settings import Settings
from fusion.application.sources import SourceService
from fusion.application.tools import ToolService
from fusion.application.views import MaterializedViewService
from fusion.domain.catalog import SchemaCatalog
from fusion.ports.analytics_store import AnalyticsStore
from fusion.ports.cache import QueryCache
from fusion.ports.scheduler import Scheduler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FusionApp:
    settings: Settings
    catalog: SchemaCatalog
    store: AnalyticsStore
    cache: QueryCache
    scheduler: Scheduler
    sources: SourceService
    query: QueryService
    views: MaterializedViewService
    backup: BackupService
    semantic: SemanticService
    tools: ToolService

    def schema_context(self, schemas: list[str] | None = None) -> str:
        """LLM-friendly Markdown description of the connected schemas."""
        return self.catalog.generate_context(schemas)

    def close(self) -> None:
        """Stop timers, close sources and the store (idempotent)."""
        self.sources.stop_auto_refresh()
        self.views.close()
        self.backup.stop()
        self.sources.close_all()
        self.scheduler.cancel_all()
        try:
            self.store.close()
        except Exception as e:
            logger.warning("Error closing store: %s", e)
        logger.info("FusionApp closed")

    def __enter__(self) -> FusionApp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
