"""The question set.

Questions live as data, not code, so the set can be extended by someone who
does not read Python — and so a run can state exactly which version of the set
it used.

Every question carries its gold SQL *per dialect*, because the point of this
benchmark is Oracle and SQL Server, where the dialects genuinely differ
(``FETCH FIRST`` versus ``TOP``, ``NVL`` versus ``ISNULL``). A single gold
query would quietly restrict the benchmark to whichever engine it was written
for.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Question:
    """One business question and how to check an answer to it."""

    id: str
    text: str
    #: ISO language code. Turkish questions are half the set on purpose: a
    #: model that only performs in English is not usable here.
    lang: str = "en"
    #: dialect -> the query that defines the right answer.
    gold: dict[str, str] = field(default_factory=dict)
    #: True when the question asks for an order ("top 5"), so row order counts.
    ordered: bool = False
    tags: tuple[str, ...] = ()

    def gold_for(self, dialect: str) -> str:
        """The gold query for ``dialect``, falling back to a shared one."""
        return self.gold.get(dialect) or self.gold.get("default", "")


@dataclass(frozen=True, slots=True)
class QuestionSet:
    """A named, versioned collection of questions."""

    name: str
    questions: tuple[Question, ...]
    schema_sql: str = ""
    #: Every table and column name, for the hallucination check.
    identifiers: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.questions)

    def __iter__(self) -> Iterator[Question]:
        return iter(self.questions)

    def by_language(self, lang: str) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.lang == lang)

    def by_tag(self, tag: str) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if tag in q.tags)


def load_question_set(path: str | Path) -> QuestionSet:
    """Load a question set from JSON.

    Raises:
        ValueError: On a duplicate question id — the ids key the paired
            comparison, so two questions sharing one would silently make the
            arms answer different sets.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = tuple(
        Question(
            id=str(entry["id"]),
            text=str(entry["text"]),
            lang=str(entry.get("lang", "en")),
            gold=dict(entry.get("gold", {})),
            ordered=bool(entry.get("ordered", False)),
            tags=tuple(entry.get("tags", ())),
        )
        for entry in data["questions"]
    )
    ids = [q.id for q in questions]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate question ids in {path}: {', '.join(duplicates)}")
    return QuestionSet(
        name=str(data.get("name", Path(path).stem)),
        questions=questions,
        schema_sql=str(data.get("schema_sql", "")),
        identifiers=tuple(data.get("identifiers", ())),
    )


def write_question_set(question_set: QuestionSet, path: str | Path) -> None:
    """Write a question set back out (used to keep the file canonical)."""
    payload: dict[str, Any] = {
        "name": question_set.name,
        "schema_sql": question_set.schema_sql,
        "identifiers": list(question_set.identifiers),
        "questions": [
            {
                "id": q.id,
                "text": q.text,
                "lang": q.lang,
                "gold": q.gold,
                "ordered": q.ordered,
                "tags": list(q.tags),
            }
            for q in question_set.questions
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")


def paired_ids(sets: Sequence[Sequence[str]]) -> list[str]:
    """Ids answered by every arm, in a stable order.

    An arm that skipped a question must not silently shrink the comparison:
    McNemar needs the same questions on both sides, so the runner intersects
    first and reports what it dropped.
    """
    if not sets:
        return []
    common = set(sets[0])
    for other in sets[1:]:
        common &= set(other)
    return [qid for qid in sets[0] if qid in common]
