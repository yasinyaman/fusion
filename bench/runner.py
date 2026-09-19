"""Running the comparison and reporting it.

The thresholds in the report are fixed before any arm runs, because a decision
rule chosen after seeing the numbers is not a decision rule.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.dataset import Question, QuestionSet, paired_ids
from bench.scoring import Answer, Grade, grade
from bench.stats import McNemarResult, accuracy_gap, mcnemar_exact, wilson_interval

#: Written down before the run, per the plan.
THRESHOLD_SEMANTIC_OVER_CATALOG = 5.0
THRESHOLD_SEMANTIC_OVER_RAW = 15.0


@dataclass(slots=True)
class ArmResult:
    """Everything one arm produced."""

    name: str
    grades: dict[str, Grade] = field(default_factory=dict)

    def outcomes(self, ids: Sequence[str]) -> list[bool]:
        return [self.grades[qid].correct for qid in ids]

    def accuracy(self, ids: Sequence[str]) -> Any:
        correct = sum(1 for qid in ids if self.grades[qid].correct)
        return wilson_interval(correct, len(ids))

    def hallucination_rate(self, ids: Sequence[str]) -> float:
        if not ids:
            return 0.0
        return sum(1 for qid in ids if self.grades[qid].hallucinated_any) / len(ids)

    def exact_match_rate(self, ids: Sequence[str]) -> float:
        if not ids:
            return 0.0
        return sum(1 for qid in ids if self.grades[qid].exact_match) / len(ids)

    def fallbacks(self, ids: Sequence[str]) -> int:
        """How many answers were not the arm's own work.

        Non-zero means a seeded arm kept its proposal because the model
        failed, and the accuracy above is a blend of two arms rather than one.
        """
        return sum(1 for qid in ids if self.grades[qid].fell_back)

    def median_latency_ms(self, ids: Sequence[str]) -> float:
        """Median, not mean: one timed-out call should not define the number."""
        values = sorted(self.grades[qid].latency_ms for qid in ids)
        if not values:
            return 0.0
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2


@dataclass(slots=True)
class Comparison:
    """One arm measured against another."""

    first: str
    second: str
    gap_points: float
    mcnemar: McNemarResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "first": self.first,
            "second": self.second,
            "gap_points": round(self.gap_points, 1),
            **self.mcnemar.as_dict(),
        }


def run_arm(
    arm: Any,
    questions: Sequence[Question],
    gold: dict[str, list[tuple]],
    dialect: str,
    identifiers: Sequence[str],
) -> ArmResult:
    """Run one arm over the question set and grade every answer."""
    result = ArmResult(name=arm.name)
    for question in questions:
        try:
            answer = arm.answer(question)
        except Exception as e:  # an arm must not be able to abort the run
            answer = Answer(error=f"arm raised: {e}")
        result.grades[question.id] = grade(
            answer,
            expected_rows=gold.get(question.id, []),
            gold_sql=question.gold_for(dialect),
            known_identifiers=identifiers,
            ordered=question.ordered,
        )
    return result


def compare(first: ArmResult, second: ArmResult, ids: Sequence[str]) -> Comparison:
    """Paired comparison of two arms over the questions both answered."""
    return Comparison(
        first=first.name,
        second=second.name,
        gap_points=accuracy_gap(first.outcomes(ids), second.outcomes(ids)),
        mcnemar=mcnemar_exact(first.outcomes(ids), second.outcomes(ids)),
    )


def verdict(semantic_vs_catalog: Comparison, semantic_vs_raw: Comparison) -> str:
    """Apply the thresholds fixed before the run.

    Deliberately mechanical: the whole reason the thresholds were written down
    in advance is so this reads them rather than re-litigating them.
    """
    if semantic_vs_catalog.gap_points < THRESHOLD_SEMANTIC_OVER_CATALOG:
        return (
            f"Stop investing in the DSL: it beats the catalog arm by only "
            f"{semantic_vs_catalog.gap_points:.1f} points "
            f"(< {THRESHOLD_SEMANTIC_OVER_CATALOG}). Put the effort into catalog "
            f"quality and text-to-SQL."
        )
    if semantic_vs_raw.gap_points > THRESHOLD_SEMANTIC_OVER_RAW:
        return (
            f"Fund the DSL through to joins and multi-stage queries: it beats "
            f"raw text-to-SQL by {semantic_vs_raw.gap_points:.1f} points "
            f"(> {THRESHOLD_SEMANTIC_OVER_RAW})."
        )
    return (
        f"Ship the P0 DSL and defer joins: it beats the catalog arm by "
        f"{semantic_vs_catalog.gap_points:.1f} points but raw text-to-SQL by only "
        f"{semantic_vs_raw.gap_points:.1f}."
    )


@dataclass(slots=True)
class Report:
    """The whole run, ready to print or store."""

    question_set: str
    dialect: str
    ids: list[str]
    dropped: list[str]
    arms: list[ArmResult]
    comparisons: list[Comparison]
    verdict: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_set": self.question_set,
            "dialect": self.dialect,
            "questions": len(self.ids),
            "dropped": self.dropped,
            "arms": [
                {
                    "name": arm.name,
                    "accuracy": arm.accuracy(self.ids).as_dict(),
                    "exact_match": round(arm.exact_match_rate(self.ids), 3),
                    "hallucination_rate": round(arm.hallucination_rate(self.ids), 3),
                    "fallbacks": arm.fallbacks(self.ids),
                    "median_latency_ms": round(arm.median_latency_ms(self.ids), 1),
                }
                for arm in self.arms
            ],
            "comparisons": [c.as_dict() for c in self.comparisons],
            "verdict": self.verdict,
            # Per question, so a failure can be read back without re-running
            # and a subset can be analysed after the fact.
            "grades": {
                arm.name: {qid: arm.grades[qid].as_dict() for qid in self.ids} for arm in self.arms
            },
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Benchmark: {self.question_set} on {self.dialect}",
            "",
            f"{len(self.ids)} questions answered by every arm.",
        ]
        if self.dropped:
            lines.append(
                f"{len(self.dropped)} dropped because not every arm answered them: "
                f"{', '.join(self.dropped)}."
            )
        # The fallback column appears only when something fell back, so a
        # clean run's report is unchanged — but a run where the model went
        # away cannot be mistaken for one where it answered.
        fell_back = {arm.name: arm.fallbacks(self.ids) for arm in self.arms}
        extra = " Fell back |" if any(fell_back.values()) else ""
        lines += [
            "",
            "| Arm | Execution accuracy (95% CI) | Hallucinated | Exact match | "
            f"Median latency |{extra}",
            "|---|---|---|---|---|" + ("---|" if extra else ""),
        ]
        for arm in self.arms:
            row = (
                f"| {arm.name} | {arm.accuracy(self.ids)} | "
                f"{arm.hallucination_rate(self.ids):.1%} | "
                f"{arm.exact_match_rate(self.ids):.1%} | "
                f"{arm.median_latency_ms(self.ids):.0f} ms |"
            )
            if extra:
                row += f" {fell_back[arm.name]} |"
            lines.append(row)
        lines += [
            "",
            "## Paired comparisons (McNemar exact)",
            "",
            "| Comparison | Gap | Only first | Only second | p |",
            "|---|---|---|---|---|",
        ]
        for c in self.comparisons:
            lines.append(
                f"| {c.first} vs {c.second} | {c.gap_points:+.1f} pts | "
                f"{c.mcnemar.only_first} | {c.mcnemar.only_second} | {c.mcnemar.p_value:.4g} |"
            )
        if self.verdict:
            lines += ["", "## Verdict", "", self.verdict]
        lines += [
            "",
            "Exact match is a diagnostic, not a score: a low exact-match with a high "
            "execution accuracy is the expected, healthy pattern, because two correct "
            "queries rarely look alike.",
        ]
        return "\n".join(lines)


def build_report(
    question_set: QuestionSet,
    dialect: str,
    results: Sequence[ArmResult],
) -> Report:
    """Assemble a report, comparing the last arm against the earlier ones."""
    ids = paired_ids([list(r.grades) for r in results])
    dropped = sorted({qid for r in results for qid in r.grades} - set(ids))
    comparisons = [compare(results[-1], other, ids) for other in results[:-1]]
    report = Report(
        question_set=question_set.name,
        dialect=dialect,
        ids=ids,
        dropped=dropped,
        arms=list(results),
        comparisons=comparisons,
    )
    if len(comparisons) >= 2:
        # Arms are ordered raw, catalog, semantic; the comparisons therefore
        # come back semantic-vs-raw first, semantic-vs-catalog second.
        report.verdict = verdict(comparisons[1], comparisons[0])
    return report


def subset_report(report: Report, ids: Sequence[str], label: str) -> Report:
    """The same run, restricted to some of its questions.

    Used to separate "the arm got it wrong" from "the arm was never able to
    answer this kind of question" — a single-fact-table DSL cannot do a join,
    and averaging those in says less than reporting both.
    """
    keep = [qid for qid in report.ids if qid in set(ids)]
    comparisons = [compare(report.arms[-1], other, keep) for other in report.arms[:-1]]
    return Report(
        question_set=f"{report.question_set} ({label})",
        dialect=report.dialect,
        ids=keep,
        dropped=[],
        arms=report.arms,
        comparisons=comparisons,
    )


def write_report(report: Report, directory: str | Path) -> tuple[Path, Path]:
    """Write the report as JSON and Markdown; returns both paths."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{report.question_set}-{report.dialect}"
    json_path = target / f"{stem}.json"
    md_path = target / f"{stem}.md"
    json_path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path
