"""Deciding whether an answer is right.

Grading generated SQL by comparing it to a reference *string* measures the
wrong thing: two correct queries rarely look alike, and two similar-looking
ones can differ on a join. So an answer is judged by the rows it produces.

Three judgements are recorded per answer, because they fail for different
reasons and a single "accuracy" number hides which:

- **execution accuracy** — the rows match the gold query's rows;
- **exact match** — the generated SQL is textually the gold query, normalized.
  Reported only as a diagnostic: a low exact-match with a high execution
  accuracy is the expected, healthy pattern;
- **hallucinated identifiers** — the answer referred to a table or column the
  schema does not have. The most useful failure signal there is, because it is
  the one a semantic layer is supposed to make impossible.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

#: Relative tolerance for float comparison. Sums of money computed in a
#: different order differ in the last bits; that is not a wrong answer.
FLOAT_TOLERANCE = 1e-6

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")

#: Words that look like identifiers but are the language, not the schema.
SQL_KEYWORDS: frozenset[str] = frozenset(
    """
    select from where group by having order limit offset as and or not in is null
    join inner left right full outer on union all distinct case when then else end
    asc desc with over partition rows range between unbounded preceding following
    current row count sum avg min max median cast interval date timestamp extract
    coalesce nullif round floor ceil abs true false lag lead rank dense_rank ntile
    percent_rank date_trunc fetch first only top offset_fetch values into set
    """.split()
)


def normalize_value(value: Any) -> Any:
    """Reduce a driver-native value to something comparable across engines.

    Oracle hands back ``Decimal`` where DuckDB hands back ``float``, and a
    ``DATE`` may arrive as a ``datetime`` at midnight. Neither difference makes
    an answer wrong.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    return value


def values_equal(left: Any, right: Any) -> bool:
    """Whether two normalized values count as the same answer."""
    left, right = normalize_value(left), normalize_value(right)
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(left, right, rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE)
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    return bool(left == right)


def rows_equal(
    actual: Sequence[Sequence[Any]],
    expected: Sequence[Sequence[Any]],
    ordered: bool = False,
) -> bool:
    """Whether two result sets answer the question the same way.

    Rows are compared by position within the row, never by column name: two
    arms may name the same computed column differently and still be right.

    ``ordered`` should be True only when the question actually asks for an
    order ("top 5 by revenue"); otherwise row order is not part of the answer
    and comparing it would fail correct results.
    """
    if len(actual) != len(expected):
        return False
    if not expected:
        return True
    if len({len(row) for row in [*actual, *expected]}) != 1:
        return False
    if ordered:
        return all(
            all(values_equal(a, b) for a, b in zip(row_a, row_b, strict=True))
            for row_a, row_b in zip(actual, expected, strict=True)
        )
    remaining = list(expected)
    for row in actual:
        for index, candidate in enumerate(remaining):
            if all(values_equal(a, b) for a, b in zip(row, candidate, strict=True)):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


def normalize_sql(sql: str) -> str:
    """Collapse whitespace and case so two spellings of one query compare equal.

    String literals keep their case — ``status = 'Paid'`` is not the same
    query as ``status = 'paid'``.
    """
    placeholders: list[str] = []

    def stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    stashed = _STRING_LITERAL.sub(stash, sql)
    collapsed = re.sub(r"\s+", " ", stashed)
    # Spacing around punctuation is not part of the query: `sum( tutar )` and
    # `sum(tutar)` are the same statement, and exact-match is only a useful
    # diagnostic if it says so.
    collapsed = re.sub(r"\(\s+", "(", collapsed)
    collapsed = re.sub(r"\s+\)", ")", collapsed)
    collapsed = re.sub(r"\s*,\s*", ", ", collapsed)
    collapsed = collapsed.strip().rstrip(";").strip().lower()
    for index, literal in enumerate(placeholders):
        collapsed = collapsed.replace(f"\x00{index}\x00", literal)
    return collapsed


def identifiers_in(sql: str) -> set[str]:
    """Identifier-looking words in a statement, minus the SQL keywords.

    Deliberately crude: it over-collects aliases, and that is the safe
    direction — a hallucination check that misses a name is worse than one
    that occasionally asks about a alias that is obviously fine.
    """
    without_strings = _STRING_LITERAL.sub(" ", sql)
    found = {word.lower() for word in _IDENTIFIER.findall(without_strings)}
    return found - SQL_KEYWORDS


def hallucinated_identifiers(sql: str, known: Iterable[str]) -> set[str]:
    """Names the statement used that the schema does not have.

    Aliases the statement itself defines are subtracted first, so
    ``SUM(amount) AS revenue`` does not read as an invented column.
    """
    text = _STRING_LITERAL.sub(" ", sql)
    alias = re.findall(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I)
    defined = {match.lower() for match in alias}
    known_lower = {name.lower() for name in known}
    return identifiers_in(sql) - known_lower - defined


@dataclass(frozen=True, slots=True)
class Grade:
    """How one answer scored."""

    correct: bool
    exact_match: bool = False
    hallucinated: tuple[str, ...] = ()
    error: str = ""
    latency_ms: float = 0.0

    @property
    def hallucinated_any(self) -> bool:
        return bool(self.hallucinated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "exact_match": self.exact_match,
            "hallucinated": list(self.hallucinated),
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """What an arm produced for one question.

    ``sql`` is whatever the arm emitted — a statement for the SQL arms, a
    structured request for a semantic one. ``named`` lets an arm state the
    identifiers it actually referenced; without it the grader has to read them
    out of ``sql``, which only makes sense when ``sql`` really is SQL.
    """

    rows: list[tuple[Any, ...]] = field(default_factory=list)
    sql: str = ""
    error: str = ""
    latency_ms: float = 0.0
    named: tuple[str, ...] | None = None
    #: Set when an arm answered with something other than what it was asked
    #: for — a seeded arm keeping its proposal after the model failed. Kept
    #: apart from `error` because the answer did run and may well be right;
    #: what it must not do is pass as the model's own work.
    fell_back: bool = False


def grade(
    answer: Answer,
    expected_rows: Sequence[Sequence[Any]],
    gold_sql: str = "",
    known_identifiers: Iterable[str] = (),
    ordered: bool = False,
) -> Grade:
    """Grade one answer against the gold result.

    An answer that failed to run is simply wrong: from the asker's point of
    view a query that errors and a query that returns the wrong rows are the
    same non-answer, and keeping the message is what makes the failure
    diagnosable afterwards.
    """
    invented = _invented(answer, known_identifiers)
    if answer.error:
        return Grade(
            correct=False,
            error=answer.error,
            latency_ms=answer.latency_ms,
            hallucinated=invented,
        )
    return Grade(
        correct=rows_equal(answer.rows, expected_rows, ordered=ordered),
        # Only meaningful when the arm emits SQL: an arm answering with a
        # structured request has no SQL text to compare against the gold.
        exact_match=bool(gold_sql)
        and answer.named is None
        and normalize_sql(answer.sql) == normalize_sql(gold_sql),
        hallucinated=invented,
        latency_ms=answer.latency_ms,
    )


def _invented(answer: Answer, known: Iterable[str]) -> tuple[str, ...]:
    """Identifiers the answer used that the schema does not have.

    An arm that states the names it referenced is taken at its word. Reading
    them out of ``sql`` instead would parse a JSON request as SQL and report
    its keys — ``table``, ``metrics`` — as invented columns, which is how this
    check first managed to report 100% for the one arm that cannot invent a
    column at all.
    """
    known_lower = {name.lower() for name in known}
    if answer.named is not None:
        return tuple(sorted({n.lower() for n in answer.named} - known_lower))
    if not answer.sql:
        return ()
    return tuple(sorted(hallucinated_identifiers(answer.sql, known_lower)))
