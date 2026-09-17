"""sqlglot-backed SqlValidator (guardrails) and SqlAnalyzer (table refs)."""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp

from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import MV_PREFIX, TableRef
from fusion.domain.sql_text import (
    find_forbidden_function,
    has_multiple_statements,
    starts_with_dangerous_keyword,
)

logger = logging.getLogger(__name__)

# Any query expression: SELECT, set operations (UNION/INTERSECT/EXCEPT) and
# parenthesised subqueries. DDL/DML/commands are not Query subclasses.
_ALLOWED_TYPES: tuple[type, ...] = (exp.Query,)


class SqlglotValidator:
    """Allows only read-only query statements.

    Layers, in order: empty check, multi-statement detection, forbidden
    function denylist (file/network/extension access), then an AST allowlist
    with a keyword fallback for SQL sqlglot cannot parse.
    """

    def __init__(self, allow_create_mv: bool = False) -> None:
        self._allow_create_mv = allow_create_mv

    def validate(self, sql: str) -> None:
        stripped = sql.strip()
        if not stripped:
            raise GuardrailViolation("Empty SQL query")

        if has_multiple_statements(stripped):
            raise GuardrailViolation(
                f"Multi-statement SQL detected (possible injection): {stripped[:100]}"
            )

        forbidden = find_forbidden_function(stripped)
        if forbidden:
            raise GuardrailViolation(
                f"Blocked forbidden function '{forbidden}()'. "
                f"File, network, and extension access is not allowed: {stripped[:100]}"
            )

        try:
            parsed = sqlglot.parse(stripped, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            keyword = starts_with_dangerous_keyword(stripped)
            if keyword:
                raise GuardrailViolation(
                    f"Blocked SQL starting with {keyword}: {stripped[:100]}"
                ) from None
            return

        if not parsed:
            raise GuardrailViolation("Failed to parse SQL query")

        for statement in parsed:
            if statement is not None:
                self._validate_statement(statement, stripped)

    def _validate_statement(self, statement: exp.Expr, original_sql: str) -> None:
        if isinstance(statement, _ALLOWED_TYPES):
            if statement.args.get("into"):
                raise GuardrailViolation(
                    f"Blocked SELECT INTO statement. Only SELECT queries are allowed: "
                    f"{original_sql[:100]}"
                )
            return

        if statement.key == "command" and original_sql.upper().startswith("EXPLAIN"):
            return

        if self._allow_create_mv and isinstance(statement, exp.Create):
            table_name = str(statement.this) if statement.this else ""
            if table_name.startswith(MV_PREFIX):
                return

        raise GuardrailViolation(
            f"Blocked {type(statement).__name__} statement. Only SELECT queries are allowed: "
            f"{original_sql[:100]}"
        )


class SqlglotAnalyzer:
    """Extracts table references and rewrites source prefixes for pushdown."""

    def table_references(self, sql: str) -> list[TableRef]:
        try:
            parsed = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            logger.warning("sqlglot failed to parse SQL; no table references extracted")
            return []

        statements = [s for s in (parsed or []) if s is not None]
        cte_names = {
            cte.alias.lower()
            for statement in statements
            for cte in statement.find_all(exp.CTE)
            if cte.alias
        }

        refs: list[TableRef] = []
        for statement in statements:
            for table in statement.find_all(exp.Table):
                name = table.name
                if not name or name.lower() in cte_names:
                    continue
                ref = TableRef(table.db or "", name)
                if ref not in refs:
                    refs.append(ref)
        return refs

    def strip_source_prefix(self, sql: str, source: str) -> str:
        try:
            parsed = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.IGNORE)
            if not parsed or parsed[0] is None:
                return sql
            for statement in parsed:
                if statement is None:
                    continue
                for table in statement.find_all(exp.Table):
                    if table.db == source:
                        table.set("db", None)
            return parsed[0].sql()
        except Exception:
            return sql.replace(f"{source}.", "")
