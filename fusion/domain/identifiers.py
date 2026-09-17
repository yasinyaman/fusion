"""Identifier rules shared by the tool layer and services."""

import re

from fusion.domain.errors import QueryError

# Max rows returned to an LLM so results fit in a context window.
MAX_RESULT_ROWS = 100

# Aggregation functions the aggregate tool may emit (whitelist for safety).
ALLOWED_AGG_FUNCS = frozenset({"SUM", "AVG", "COUNT", "MIN", "MAX"})

# Safe SQL identifier (optionally dotted, e.g. source.table). Prevents injection
# through table/column names supplied by an LLM.
IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")

# Materialized view names: no dots.
VIEW_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str, label: str = "identifier") -> None:
    """Raise QueryError unless ``name`` is a safe (dotted) identifier."""
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise QueryError(
            f"Invalid {label}: '{name}'. Only alphanumeric, underscore, and dot allowed."
        )


def is_valid_view_name(name: str) -> bool:
    return isinstance(name, str) and VIEW_NAME_RE.match(name) is not None
