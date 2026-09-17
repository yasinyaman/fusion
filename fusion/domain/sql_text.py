"""Text-level SQL helpers: literal/comment stripping, denylists, cache keys.

These are deliberately parser-free so they work on SQL that sqlglot cannot
parse, and so the cache key does not depend on a third-party AST.
"""

from __future__ import annotations

import re

# Keywords that indicate destructive statements (fallback when parsing fails).
DANGEROUS_KEYWORDS = frozenset(
    {
        "DROP",
        "DELETE",
        "INSERT",
        "UPDATE",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "CREATE",
        "REPLACE",
    }
)

# DuckDB functions that reach the local filesystem, the network, or the
# extension loader. They parse as ordinary functions inside a valid SELECT, so
# a statement-type allowlist alone does not catch them. Defense-in-depth on top
# of the store's ``enable_external_access=FALSE`` latch.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "parquet_scan",
        "read_json",
        "read_json_auto",
        "read_json_objects",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_text",
        "read_blob",
        "glob",
        "sniff_csv",
        "delta_scan",
        "iceberg_scan",
        "iceberg_metadata",
        "iceberg_snapshots",
        "postgres_scan",
        "postgres_query",
        "mysql_scan",
        "mysql_query",
        "sqlite_scan",
        "install",
        "load",
    }
)

FORBIDDEN_FUNCTION_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in sorted(FORBIDDEN_FUNCTIONS)) + r")\s*\(",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")


def strip_string_literals(sql: str) -> str:
    """Remove single- and double-quoted segments (quotes included)."""
    out: list[str] = []
    in_single = in_double = False
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            out.append(ch)
    return "".join(out)


def strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def clean_sql(sql: str) -> str:
    """SQL with string literals and comments removed (for structural checks)."""
    return strip_comments(strip_string_literals(sql))


def has_multiple_statements(sql: str) -> bool:
    """True if more than one ``;``-separated statement is present."""
    parts = [p.strip() for p in clean_sql(sql).split(";") if p.strip()]
    return len(parts) > 1


def find_forbidden_function(sql: str) -> str | None:
    """Name of the first forbidden function call, or None."""
    match = FORBIDDEN_FUNCTION_RE.search(clean_sql(sql))
    return match.group(1).lower() if match else None


def starts_with_dangerous_keyword(sql: str) -> str | None:
    """The dangerous keyword the statement starts with, or None."""
    upper = sql.upper().lstrip()
    for keyword in DANGEROUS_KEYWORDS:
        if upper.startswith(keyword):
            return keyword
    return None


def normalize_sql_for_cache(sql: str) -> str:
    """Canonical form for cache keys.

    Whitespace runs collapse to one space and keywords/identifiers are
    upper-cased *outside* quoted segments; quoted segments (string literals
    and quoted identifiers) are kept verbatim so ``'alice'`` and ``'ALICE'``
    never share a cache entry.
    """
    out: list[str] = []
    segment: list[str] = []
    in_single = in_double = False

    def flush() -> None:
        if segment:
            out.append(_WS_RE.sub(" ", "".join(segment)).upper())
            segment.clear()

    for ch in sql:
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            out.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            flush()
            out.append(ch)
            in_single = True
        elif ch == '"':
            flush()
            out.append(ch)
            in_double = True
        else:
            segment.append(ch)
    flush()
    return "".join(out).strip()
