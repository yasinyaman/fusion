"""Port: turning a semantic query into SQL."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fusion.domain.models import TableRef
from fusion.domain.semantic_query import CompiledQuery, SemanticQuery


@runtime_checkable
class SemanticCompiler(Protocol):
    """Generates the SQL that answers a semantic query."""

    def compile(
        self, query: SemanticQuery, tables: Mapping[TableRef, str] | None = None
    ) -> CompiledQuery:
        """SQL for ``query``, reading each table from ``tables`` or its own name.

        ``tables`` maps a table to where it actually lives in the store — a
        slice, usually, whose name encodes the rows it holds. The compiler
        writes those names in directly rather than emitting the original name
        and having someone rewrite it afterwards: the SQL is generated for the
        store's dialect, and re-parsing it under a neutral one rewrites string
        literals and drops quoting.

        Implementations must emit a single SELECT statement that passes
        ``SqlValidator.validate``, quote every identifier, and never inline a
        value that did not come from the query's own literals.
        """
        ...
