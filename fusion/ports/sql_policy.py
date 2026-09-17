"""Outbound ports for SQL validation and analysis (sqlglot in prod)."""

from __future__ import annotations

from typing import Protocol

from fusion.domain.models import TableRef


class SqlValidator(Protocol):
    def validate(self, sql: str) -> None:
        """Raise GuardrailViolation unless ``sql`` is a safe read-only query."""
        ...


class SqlAnalyzer(Protocol):
    def table_references(self, sql: str) -> list[TableRef]:
        """Tables referenced by ``sql`` (CTE names excluded).

        Unqualified names come back with an empty ``source``.
        """
        ...

    def strip_source_prefix(self, sql: str, source: str) -> str:
        """Rewrite ``source.table`` references to bare ``table`` (for pushdown)."""
        ...
