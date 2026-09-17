"""Materialized view rules: refresh intervals and priorities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fusion.domain.models import MV_PREFIX

PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}

_EVERY_RE = re.compile(r"every\s+(\d+)\s+(second|minute|hour|day)s?")
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(slots=True)
class ViewSpec:
    """A materialized view definition and its refresh bookkeeping."""

    name: str
    sql: str
    refresh: str = "manual"
    priority: str = "normal"
    created_at: float = 0.0
    last_refresh: float = 0.0

    @property
    def table_name(self) -> str:
        return f"{MV_PREFIX}{self.name}"

    @property
    def priority_rank(self) -> int:
        return PRIORITY_ORDER.get(self.priority, PRIORITY_ORDER["normal"])

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "table_name": self.table_name,
            "refresh": self.refresh,
            "priority": self.priority,
            "last_refresh": self.last_refresh,
        }


def parse_refresh_interval(refresh: str) -> int:
    """Seconds between refreshes; ``0`` means manual/unknown.

    Accepts ``manual``, ``hourly``, ``daily`` and ``every N seconds|minutes|hours|days``.
    """
    text = refresh.lower().strip()
    if text == "manual":
        return 0
    if text == "hourly":
        return 3600
    if text == "daily":
        return 86400
    match = _EVERY_RE.match(text)
    if match:
        return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    return 0
