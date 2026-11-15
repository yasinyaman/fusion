"""Why an arm lost, read back from a finished run.

An accuracy number says an arm was wrong; it does not say whether the layer
failed or the model chose badly. Those call for opposite responses — fix the
layer, or use a better model — so the report separates them.

Reads the stored grades, so it needs no re-run.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any

#: Ordered: the first pattern that matches an error decides the category.
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("model produced no usable request", ("model failed",)),
    (
        "the language was written wrongly",
        ("Unknown transform", "Cannot parse metric", "Unknown aggregation", "must be a"),
    ),
    (
        "named something that does not exist",
        ("Unknown measure", "Unknown dimension", "has no column", "Unknown table"),
    ),
    ("refused: too much data", ("Refusing to load",)),
    ("the query would not run", ("execution failed", "Query execution failed")),
]


def categorise(grade: dict[str, Any]) -> str:
    """One reason an answer was wrong."""
    if grade["correct"]:
        return "correct"
    error = grade.get("error") or ""
    if not error:
        # It ran and returned something — the layer did its job and the model
        # asked for the wrong thing.
        return "ran, but answered the wrong question"
    for label, needles in CATEGORIES:
        if any(needle in error for needle in needles):
            return label
    return "other"


def breakdown(path: str | Path) -> dict[str, collections.Counter[str]]:
    """Per arm, how its answers fell out."""
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        arm: collections.Counter(categorise(grade) for grade in grades.values())
        for arm, grades in report.get("grades", {}).items()
    }


def to_markdown(path: str | Path) -> str:
    """The breakdown as a table."""
    counts = breakdown(path)
    reasons = sorted({reason for c in counts.values() for reason in c})
    arms = list(counts)
    lines = [
        "## Where each arm's answers went",
        "",
        "| Outcome | " + " | ".join(arms) + " |",
        "|---" * (len(arms) + 1) + "|",
    ]
    for reason in ["correct", *[r for r in reasons if r != "correct"]]:
        row = " | ".join(str(counts[arm].get(reason, 0)) for arm in arms)
        lines.append(f"| {reason} | {row} |")
    lines += [
        "",
        '"Ran, but answered the wrong question" is the important row: the layer '
        "did its job and the model asked for the wrong thing. That is a case "
        "for a better model, not a different layer.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "bench/results/banka-tr-en-duckdb.json"
    print(to_markdown(target))
