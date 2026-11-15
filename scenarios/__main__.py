"""Command line: generate the data, list the scenarios, run them.

python -m scenarios generate --scale 0.1
python -m scenarios list
python -m scenarios run --scale 0.1 --sector banking
python -m scenarios run --scale 1 --only B03,T02
python -m scenarios export --scale 0.1          # Postgres/MySQL + Warp stack
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from scenarios import harness, report
from scenarios.synth import ALL_DATABASES, SECTORS, generate

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE / ".data"
DEFAULT_RESULTS = HERE / "results"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _generate(args: argparse.Namespace) -> int:
    print(f"Generating scale {args.scale:g} into {args.data}")
    started = time.perf_counter()
    target = generate(ALL_DATABASES, args.scale, args.data, force=args.force, say=print)
    total = sum(t.rows(args.scale) for db in ALL_DATABASES for t in db.tables)
    print(
        f"{total:,} rows across {len(ALL_DATABASES)} databases in "
        f"{time.perf_counter() - started:.1f}s -> {target}"
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    chosen = harness.select(sectors=_csv(args.sector), capability=args.capability)
    sector = None
    for item in chosen:
        if item.sector != sector:
            sector = item.sector
            print(f"\n{sector.upper()}")
        print(f"  {item.id:5s} {item.title}")
        if args.verbose:
            print(f"        {item.story}")
            print(
                f"        sources: {', '.join(item.sources)}; "
                f"exercises: {', '.join(item.capabilities)}"
            )
    print(f"\n{len(chosen)} scenarios")
    return 0


def _run(args: argparse.Namespace) -> int:
    chosen = harness.select(_csv(args.only), _csv(args.sector), args.capability)
    if not chosen:
        print("No scenario matches.")
        return 2
    generate(ALL_DATABASES, args.scale, args.data, say=lambda _m: None)
    urls = {"*": args.warp_url}
    if args.warp_sql_url:
        urls.update({db.name: args.warp_sql_url for db in ALL_DATABASES if db.pushdown})
    config = harness.RunConfig(
        scale=args.scale,
        root=args.data,
        mode=args.mode,
        warp_urls=urls,
        warp_api_key=args.warp_api_key,
        memory_limit=args.memory_limit,
        threads=args.threads,
        latency_ms=args.latency_ms,
    )
    print(f"Running {len(chosen)} scenarios at scale {args.scale:g} ({args.mode})")
    results = harness.run(chosen, config)
    summary = report.totals(results)
    markdown, payload = report.write(results, config, args.results)
    print(
        f"\n{summary['passed']}/{summary['scenarios']} scenarios passed, "
        f"{summary['steps'] - summary['steps_failed']}/{summary['steps']} steps, "
        f"{summary['skipped']} skipped, {summary['seconds']:.1f}s, "
        f"peak RSS {summary['peak_rss_mb']:,.0f} MB"
    )
    print(f"Report: {markdown}\n        {payload}")
    return 0 if summary["failed"] == 0 else 1


def _export(args: argparse.Namespace) -> int:
    from scenarios.synth import export

    generate(ALL_DATABASES, args.scale, args.data, say=print)
    target = export.export(ALL_DATABASES, args.scale, args.data, args.out, say=print)
    print(f"Stack written to {target}; see its README for how to start it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scenarios", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scale", type=float, default=0.1, help="1.0 is about 61M rows")
        p.add_argument("--data", type=Path, default=DEFAULT_ROOT, help="where the databases live")

    p = sub.add_parser("generate", help="synthesize the source databases")
    common(p)
    p.add_argument("--force", action="store_true", help="rebuild databases that already exist")
    p.set_defaults(fn=_generate)

    p = sub.add_parser("list", help="show the scenarios")
    p.add_argument("--sector", default="", help=f"comma list of: {', '.join(SECTORS)}, platform")
    p.add_argument("--capability", default="")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=_list)

    p = sub.add_parser("run", help="run scenarios and write a report")
    common(p)
    p.add_argument("--sector", default="")
    p.add_argument("--only", default="", help="comma list of scenario ids")
    p.add_argument("--capability", default="")
    p.add_argument("--mode", choices=("inproc", "warp"), default="inproc")
    p.add_argument("--warp-url", default="http://localhost:8000")
    p.add_argument("--warp-sql-url", default="", help="Warp that allows raw SQL (pushdown)")
    p.add_argument("--warp-api-key", default="")
    p.add_argument("--memory-limit", default="4GB")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument(
        "--latency-ms",
        type=float,
        default=0.0,
        help="in-process only: delay every source call, as a network would",
    )
    p.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    p.add_argument("--log", default="WARNING", help="log level for fusion's own logs")
    p.set_defaults(fn=_run)

    p = sub.add_parser("export", help="write a Postgres + MySQL + Warp stack with the data")
    common(p)
    p.add_argument("--out", type=Path, default=HERE / ".stack")
    p.set_defaults(fn=_export)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(args, "log", "WARNING").upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
