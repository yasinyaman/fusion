"""Outbound ports for SQL validation and analysis (sqlglot in prod)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from fusion.domain.models import TableRef
from fusion.domain.query_shape import QueryShape


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

    def analyze(self, sql: str) -> QueryShape:
        """Per-table columns, predicates and join keys of ``sql``.

        Implementations must be conservative: anything they cannot attribute
        to exactly one table, or that could change the result if evaluated at
        the source (OR, NOT, functions, parameters, the null side of an outer
        join), is left out. SQL they do not fully understand comes back as
        ``QueryShape(is_simple_select=False)``.
        """
        ...

    def rewrite_tables(self, sql: str, mapping: Mapping[TableRef, str]) -> str:
        """Point table references at other tables, keeping the original names as aliases.

        Used to run a query against loaded slices: ``FROM db.orders o``
        becomes ``FROM db.orders__s_abc123 o``, and an unaliased
        ``FROM db.orders`` gains the alias ``orders`` so that ``orders.id``
        still resolves.
        """
        ...
