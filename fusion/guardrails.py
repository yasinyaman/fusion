"""SQL guardrails for blocking destructive queries."""

import re

import sqlglot
from sqlglot import exp

from fusion.exceptions import GuardrailViolation

# Statement types that are allowed to pass through guardrails
_ALLOWED_TYPES = (
    exp.Select,
)

# Keywords that indicate destructive operations
_DANGEROUS_KEYWORDS = {
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "CREATE", "REPLACE",
}

# DuckDB functions that reach the local filesystem, network, or extension
# loader. These parse as ordinary functions inside an otherwise-valid SELECT
# (e.g. `SELECT * FROM read_csv('/etc/passwd')`), so the statement-type
# allowlist alone does not catch them. This is defense-in-depth on top of the
# engine's `enable_external_access=FALSE` latch.
_FORBIDDEN_FUNCTIONS = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan",
    "read_json", "read_json_auto", "read_json_objects", "read_ndjson",
    "read_ndjson_auto", "read_ndjson_objects", "read_text", "read_blob",
    "glob", "sniff_csv", "delta_scan", "iceberg_scan", "iceberg_metadata",
    "iceberg_snapshots", "postgres_scan", "postgres_query", "mysql_scan",
    "mysql_query", "sqlite_scan", "install", "load",
})

# Matches a forbidden function name immediately followed by `(`, case-insensitive.
_FORBIDDEN_FN_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in sorted(_FORBIDDEN_FUNCTIONS)) + r")\s*\(",
    re.IGNORECASE,
)


class SQLGuardrails:
    """Validates SQL queries to ensure only safe read operations are executed.

    Uses sqlglot AST-based analysis to detect and block destructive SQL
    statements like DROP, DELETE, INSERT, UPDATE, ALTER, TRUNCATE.
    """

    def __init__(self, allow_create_mv: bool = False):
        self._allow_create_mv = allow_create_mv

    def validate(self, sql: str) -> bool:
        """Validate SQL query and raise GuardrailViolation if unsafe.

        Returns True if the query is safe to execute.
        """
        sql_stripped = sql.strip()
        if not sql_stripped:
            raise GuardrailViolation("Empty SQL query")

        # Check for multi-statement attacks (semicolons)
        if self._has_multiple_statements(sql_stripped):
            raise GuardrailViolation(
                f"Multi-statement SQL detected (possible injection): {sql_stripped[:100]}"
            )

        # Block file/network/extension functions even inside a valid SELECT
        self._check_forbidden_functions(sql_stripped)

        # Try AST-based analysis first
        try:
            parsed = sqlglot.parse(sql_stripped, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            # If parsing fails, fall back to keyword check
            self._keyword_check(sql_stripped)
            return True

        if not parsed:
            raise GuardrailViolation("Failed to parse SQL query")

        for statement in parsed:
            if statement is None:
                continue
            self._validate_statement(statement, sql_stripped)

        return True

    def _validate_statement(self, statement: exp.Expression, original_sql: str) -> None:
        """Validate a single parsed SQL statement."""
        # Allow SELECT and CTE (WITH ... SELECT)
        if isinstance(statement, _ALLOWED_TYPES):
            return

        # Allow EXPLAIN
        if statement.key == "command" and original_sql.strip().upper().startswith("EXPLAIN"):
            return

        # Allow CREATE TABLE for materialized views if enabled
        if self._allow_create_mv and isinstance(statement, exp.Create):
            table_name = str(statement.this) if statement.this else ""
            if table_name.startswith("mv_"):
                return

        # Everything else is blocked
        stmt_type = type(statement).__name__
        raise GuardrailViolation(
            f"Blocked {stmt_type} statement. Only SELECT queries are allowed: "
            f"{original_sql[:100]}"
        )

    def _has_multiple_statements(self, sql: str) -> bool:
        """Detect multiple SQL statements (potential injection)."""
        # Remove string literals to avoid false positives on semicolons in strings
        cleaned = self._remove_string_literals(sql)
        # Remove comments
        cleaned = self._remove_comments(cleaned)
        # Check for semicolons that separate statements
        parts = [p.strip() for p in cleaned.split(";") if p.strip()]
        return len(parts) > 1

    def _check_forbidden_functions(self, sql: str) -> None:
        """Block dangerous DuckDB functions (file/network/extension access).

        Runs on the SQL with string literals and comments stripped, so that
        a value like ``WHERE note = 'read_csv('`` is not a false positive and
        ``read_csv/**/(...)`` cannot hide the call.
        """
        cleaned = self._remove_comments(self._remove_string_literals(sql))
        match = _FORBIDDEN_FN_RE.search(cleaned)
        if match:
            raise GuardrailViolation(
                f"Blocked forbidden function '{match.group(1).lower()}()'. "
                f"File, network, and extension access is not allowed: {sql[:100]}"
            )

    def _keyword_check(self, sql: str) -> None:
        """Fallback keyword-based check when AST parsing fails."""
        upper_sql = sql.upper().strip()
        for keyword in _DANGEROUS_KEYWORDS:
            if upper_sql.startswith(keyword):
                raise GuardrailViolation(
                    f"Blocked SQL starting with {keyword}: {sql[:100]}"
                )

    @staticmethod
    def _remove_string_literals(sql: str) -> str:
        """Remove string literals from SQL to avoid false positives."""
        result = []
        in_single = False
        in_double = False
        i = 0
        while i < len(sql):
            ch = sql[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                result.append(ch)
            i += 1
        return "".join(result)

    @staticmethod
    def _remove_comments(sql: str) -> str:
        """Remove SQL comments (-- and /* */)."""
        result = []
        i = 0
        while i < len(sql):
            # Line comment
            if sql[i : i + 2] == "--":
                while i < len(sql) and sql[i] != "\n":
                    i += 1
                continue
            # Block comment
            if sql[i : i + 2] == "/*":
                end = sql.find("*/", i + 2)
                i = end + 2 if end != -1 else len(sql)
                continue
            result.append(sql[i])
            i += 1
        return "".join(result)
