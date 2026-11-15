"""Write a run's results as Markdown (for people) and JSON (for diffing runs)."""

from __future__ import annotations

import json
import platform
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from scenarios.harness import RunConfig, ScenarioResult


def _ms(value: float) -> str:
    return f"{value:,.0f} ms" if value >= 10 else f"{value:.1f} ms"


def totals(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    ran = [r for r in results if not r.skipped]
    steps = [s for r in ran for s in r.steps]
    return {
        "scenarios": len(ran),
        "passed": sum(1 for r in ran if r.passed),
        "failed": sum(1 for r in ran if not r.passed),
        "skipped": sum(1 for r in results if r.skipped),
        "steps": len(steps),
        "steps_failed": sum(1 for s in steps if not s.ok),
        "seconds": round(sum(r.seconds for r in ran), 2),
        "peak_rss_mb": round(max((r.peak_rss_mb for r in ran), default=0.0), 1),
    }


def to_markdown(results: Sequence[ScenarioResult], config: RunConfig) -> str:
    import duckdb

    from fusion import __version__

    summary = totals(results)
    lines = [
        "# Fusion sector scenarios",
        "",
        f"- Run: {datetime.now().isoformat(timespec='seconds')}",
        f"- Fusion {__version__}, DuckDB {duckdb.__version__}, Python {platform.python_version()},"
        f" {platform.system()} {platform.machine()}",
        f"- Scale {config.scale:g}, mode `{config.mode}`, memory limit {config.memory_limit},"
        f" {config.threads} threads",
        f"- **{summary['passed']}/{summary['scenarios']} scenarios passed**"
        f" ({summary['steps'] - summary['steps_failed']}/{summary['steps']} steps),"
        f" {summary['skipped']} skipped, {summary['seconds']:.1f}s,"
        f" peak RSS {summary['peak_rss_mb']:,.0f} MB",
        "",
        "| | Id | Sector | Scenario | Steps | Time | Rows at source | Rows moved |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        item = result.scenario
        if result.skipped:
            lines.append(f"| – | {item.id} | {item.sector} | {item.title} | skipped | | | |")
            continue
        ok = sum(1 for s in result.steps if s.ok)
        lines.append(
            f"| {'✅' if result.passed else '❌'} | {item.id} | {item.sector} | {item.title} "
            f"| {ok}/{len(result.steps)} | {result.seconds:.2f}s | {result.source_rows:,} "
            f"| {result.moved:,} |"
        )

    failed = [r for r in results if not r.skipped and not r.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for result in failed:
            lines.append(f"### {result.scenario.id} — {result.scenario.title}")
            if result.error:
                lines += ["", "```", result.error.strip(), "```"]
            for step in result.failures:
                lines.append(f"- **{step.label}** ({step.kind}): {step.detail}")
            lines.append("")

    lines += ["", "## Scenarios", ""]
    for result in results:
        item = result.scenario
        if result.skipped:
            continue
        lines += [
            f"### {item.id} — {item.title}",
            "",
            item.story,
            "",
            f"Sources: {', '.join(f'`{s}`' for s in item.sources)}"
            + (f" · exercises: {', '.join(item.capabilities)}" if item.capabilities else ""),
            "",
            "| | Step | Kind | Fusion | Oracle | Rows | Moved | Plan / detail |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for step in result.steps:
            plan = ", ".join(f"{t.split('.')[-1]}→{a}" for t, a in step.actions.items())
            detail = " — ".join(part for part in (plan, step.detail) if part)
            lines.append(
                f"| {'✓' if step.ok else '✗'} | {step.label} | {step.kind} "
                f"| {_ms(step.ms) if step.ms else ''} "
                f"| {_ms(step.oracle_ms) if step.oracle_ms else ''} "
                f"| {'' if step.rows is None else f'{step.rows:,}'} "
                f"| {'' if step.moved is None else f'{step.moved:,}'} "
                f"| {detail.replace('|', '\\|')[:240]} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def write(
    results: Sequence[ScenarioResult], config: RunConfig, directory: Path
) -> tuple[Path, Path]:
    """Write ``report.md`` and ``report.json`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-{config.mode}-scale-{config.scale:g}"
    markdown = directory / f"{stem}.md"
    payload = directory / f"{stem}.json"
    markdown.write_text(to_markdown(results, config), encoding="utf-8")
    payload.write_text(
        json.dumps(
            {
                "scale": config.scale,
                "mode": config.mode,
                "totals": totals(results),
                "scenarios": [r.as_dict() for r in results],
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return markdown, payload
