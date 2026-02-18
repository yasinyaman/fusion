"""Materialized view management with auto-refresh."""

import logging
import re
import threading
import time
from typing import Any

from fusion.exceptions import QueryError
from fusion.result import QueryResult

logger = logging.getLogger(__name__)

PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}


class MaterializedViewManager:
    """Manages materialized views in DuckDB with scheduled auto-refresh.

    Creates physical tables (mv_{name}) that cache pre-computed aggregations,
    with configurable refresh intervals and priority levels.
    """

    def __init__(self, engine: Any):  # OLAPEngine (avoid circular import)
        self._engine = engine
        self._views: dict[str, dict] = {}
        self._timers: dict[str, threading.Timer] = {}

    def create(
        self,
        name: str,
        sql: str,
        refresh: str = "manual",
        priority: str = "normal",
    ) -> None:
        """Create a materialized view.

        Args:
            name: View name (will be stored as mv_{name})
            sql: SELECT query to materialize
            refresh: Refresh interval ("manual", "every 15 minutes", "hourly", "daily")
            priority: Refresh priority ("critical", "high", "normal", "low")
        """
        table_name = f"mv_{name}"

        # Validate the SQL through guardrails
        self._engine.guardrails.validate(sql)

        # Create the materialized view as a table
        try:
            self._engine.execute_raw(
                f"CREATE OR REPLACE TABLE {table_name} AS ({sql})"
            )
        except Exception as e:
            raise QueryError(f"Failed to create materialized view '{name}': {e}") from e

        self._views[name] = {
            "sql": sql,
            "table_name": table_name,
            "refresh": refresh,
            "priority": priority,
            "created_at": time.time(),
            "last_refresh": time.time(),
        }

        # Schedule auto-refresh if not manual
        if refresh != "manual":
            interval = self._parse_refresh_interval(refresh)
            if interval > 0:
                self._schedule_refresh(name, interval)

        logger.info("Created materialized view '%s' (refresh=%s)", name, refresh)

    def refresh(self, name: str, force: bool = False) -> None:
        """Refresh a materialized view."""
        if name not in self._views:
            raise QueryError(f"Materialized view '{name}' not found")

        view = self._views[name]
        try:
            self._engine.execute_raw(
                f"CREATE OR REPLACE TABLE {view['table_name']} AS ({view['sql']})"
            )
            view["last_refresh"] = time.time()
            logger.info("Refreshed materialized view '%s'", name)
        except Exception as e:
            raise QueryError(f"Failed to refresh view '{name}': {e}") from e

    def refresh_all(self, force: bool = False) -> None:
        """Refresh all views ordered by priority."""
        sorted_views = sorted(
            self._views.items(),
            key=lambda x: PRIORITY_ORDER.get(x[1]["priority"], 2),
        )
        for name, _ in sorted_views:
            try:
                self.refresh(name, force=force)
            except Exception as e:
                logger.error("Failed to refresh view '%s': %s", name, e)
                if force:
                    raise

    def drop(self, name: str) -> None:
        """Drop a materialized view."""
        if name not in self._views:
            raise QueryError(f"Materialized view '{name}' not found")

        view = self._views[name]

        # Cancel timer
        if name in self._timers:
            self._timers[name].cancel()
            del self._timers[name]

        try:
            self._engine.execute_raw(f"DROP TABLE IF EXISTS {view['table_name']}")
        except Exception:
            pass

        del self._views[name]
        logger.info("Dropped materialized view '%s'", name)

    def get(self, name: str) -> QueryResult:
        """Query a materialized view."""
        if name not in self._views:
            raise QueryError(f"Materialized view '{name}' not found")

        table_name = self._views[name]["table_name"]
        return self._engine.sql(f"SELECT * FROM {table_name}", use_cache=False)

    def list_views(self) -> list[dict]:
        """List all materialized views with metadata."""
        result = []
        for name, info in self._views.items():
            result.append({
                "name": name,
                "table_name": info["table_name"],
                "refresh": info["refresh"],
                "priority": info["priority"],
                "last_refresh": info["last_refresh"],
            })
        return result

    def _parse_refresh_interval(self, refresh: str) -> int:
        """Parse refresh interval string to seconds."""
        refresh = refresh.lower().strip()
        if refresh == "manual":
            return 0
        if refresh == "hourly":
            return 3600
        if refresh == "daily":
            return 86400

        # Parse "every X minutes/hours/seconds"
        match = re.match(r"every\s+(\d+)\s+(second|minute|hour|day)s?", refresh)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            multipliers = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
            return value * multipliers.get(unit, 60)

        logger.warning("Unknown refresh interval '%s', defaulting to manual", refresh)
        return 0

    def _schedule_refresh(self, name: str, interval: int) -> None:
        """Schedule periodic refresh for a view."""
        def _refresh_loop():
            if name in self._views:
                try:
                    self.refresh(name)
                except Exception as e:
                    logger.error("Scheduled refresh failed for '%s': %s", name, e)
                # Reschedule
                if name in self._views:
                    timer = threading.Timer(interval, _refresh_loop)
                    timer.daemon = True
                    timer.start()
                    self._timers[name] = timer

        timer = threading.Timer(interval, _refresh_loop)
        timer.daemon = True
        timer.start()
        self._timers[name] = timer

    def close(self) -> None:
        """Cancel all scheduled refreshes."""
        for name, timer in self._timers.items():
            timer.cancel()
        self._timers.clear()
