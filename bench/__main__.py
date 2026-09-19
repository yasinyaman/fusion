"""Run the benchmark: ``python -m bench --help``.

Model access is configured, not assumed: point ``--model`` at a callable and
the harness does the rest. That is deliberate — a benchmark wired to one SDK
is a benchmark nobody re-runs with a different model.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from bench.arms import TextToSqlArm
from bench.dataset import load_question_set
from bench.runner import build_report, run_arm, write_report


def _load(path: str, method: str) -> Any:
    """Import ``module:attribute`` and return something with ``method``.

    The attribute may be the object itself or a factory for it — a database
    connection usually has to be opened at call time rather than at import —
    so a callable that does not already provide ``method`` is called once.

    Raises:
        SystemExit: With what was wrong, because this is a CLI and a traceback
            about a missing attribute would not say which flag to fix.
    """
    module_name, _, attribute = path.partition(":")
    if not attribute:
        raise SystemExit(f"Expected 'module:attribute', got {path!r}")
    try:
        loaded = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as e:
        raise SystemExit(f"Could not load {path!r}: {e}") from e
    if not hasattr(loaded, method) and callable(loaded):
        loaded = loaded()
    if not hasattr(loaded, method):
        raise SystemExit(f"{path!r} has no .{method}(); it cannot be used here.")
    return loaded


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the arms and write the report."""
    parser = argparse.ArgumentParser(prog="bench", description=__doc__)
    parser.add_argument("--questions", default="bench/questions/banka-tr-en.json")
    parser.add_argument(
        "--dialect", required=True, choices=["oracle", "mssql", "duckdb", "default"]
    )
    parser.add_argument(
        "--model",
        required=True,
        help="module:attribute of an object with .complete(prompt) -> str",
    )
    parser.add_argument(
        "--executor",
        required=True,
        help="module:attribute of an object with .run(sql) -> rows",
    )
    parser.add_argument(
        "--catalog-context",
        default="",
        help="Path to the x-llm-context text for arm B (defaults to the bare DDL)",
    )
    parser.add_argument("--out", default="bench/results")
    args = parser.parse_args(argv)

    questions = load_question_set(args.questions)
    model = _load(args.model, "complete")
    executor = _load(args.executor, "run")

    gold: dict[str, list[tuple]] = {}
    for question in questions:
        sql = question.gold_for(args.dialect)
        if not sql:
            raise SystemExit(f"{question.id} has no gold query for {args.dialect}")
        gold[question.id] = executor.run(sql)

    catalog_context = (
        open(args.catalog_context, encoding="utf-8").read()  # noqa: SIM115
        if args.catalog_context
        else questions.schema_sql
    )
    arms = [
        TextToSqlArm("raw", model, executor, questions.schema_sql, args.dialect),
        TextToSqlArm("catalog", model, executor, catalog_context, args.dialect),
    ]
    results = [
        run_arm(arm, tuple(questions), gold, args.dialect, questions.identifiers) for arm in arms
    ]
    report = build_report(questions, args.dialect, results)
    json_path, md_path = write_report(report, args.out)
    print(report.to_markdown())
    print(f"\nWritten to {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
