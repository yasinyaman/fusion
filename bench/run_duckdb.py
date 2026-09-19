"""A complete three-arm run on DuckDB with a local model.

Everything here is fixed so the run is reproducible: the fixture, the question
set, temperature 0 and a seed. Re-running it on the same machine gives the
same report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

from bench.arms import LexiconArm, RepairArm, SemanticArm, TextToSqlArm
from bench.dataset import load_question_set
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    as_records,
    load_duckdb,
)
from bench.fusion_source import fixture_factory
from bench.lexicon_gen import load as load_lexicon
from bench.models import DuckDBExecutor, OllamaModel
from bench.runner import build_report, run_arm, subset_report, write_report

#: A lexicon a model wrote for this schema, if one has been generated. It is
#: checked in rather than regenerated per run, because a generated lexicon is
#: meant to be reviewed before it is used — and because a run that re-derives
#: its own vocabulary is not reproducible.
LEXICON_PATH = "bench/lexicons/banka-7b.json"

# What arm B gets and arm A does not: the catalog's own words for the columns.
CATALOG_CONTEXT = """Schema with catalog annotations (x-llm-context):

musteriler — bank customers, one row per customer (5 rows)
  musteri_id    integer  primary key
  ad_soyad      varchar  customer full name
  eposta        varchar  email address, NULL when not provided
  sube_kodu     varchar  branch code, e.g. IST01
  segment       varchar  customer segment: premium | standart | temel
  acilis_tarihi date     account opening date

islemler — transactions, one row per transaction (10 rows)
  islem_id     integer  primary key
  musteri_id   integer  -> musteriler.musteri_id (many-to-one)
  tutar        double   transaction amount (the measure to sum/average)
  para_birimi  varchar  currency: TRY | USD | EUR
  islem_turu   varchar  transaction type: havale | odeme | transfer
  durum        varchar  status: basarili (successful) | iptal (cancelled)
  islem_tarihi date     transaction date
  kanal        varchar  channel: mobil | web | sube
"""


def main(model_name: str = "qwen2.5-coder:1.5b", out: str = "bench/results") -> int:
    questions = load_question_set("bench/questions/banka-tr-en.json")
    connection = load_duckdb(duckdb.connect())
    executor = DuckDBExecutor(connection)
    model = OllamaModel(model=model_name)
    # The semantic arms answer with a structured request, so their decoding
    # is constrained to JSON; the SQL arms need no such help.
    structured = OllamaModel(model=model_name, json_only=True)

    gold = {q.id: executor.run(q.gold_for("duckdb")) for q in questions}

    tables = {
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    from fusion import Settings, build_app

    fusion = build_app(Settings(), source_factory=fixture_factory(tables))
    fusion.sources.connect("banka", {"type": "fixture"})

    tables_under_test = ["banka.islemler", "banka.musteriler"]
    lexicon = load_lexicon(LEXICON_PATH) if Path(LEXICON_PATH).exists() else None
    # The unconstrained semantic arm stays so that the cost of the DSL's
    # syntax is still visible as a paired number rather than disappearing into
    # a re-run. The constrained arm goes last because `build_report` reads the
    # verdict off the final one, and the pre-registered thresholds are about
    # the DSL at its best.
    arms = [
        TextToSqlArm("raw", model, executor, questions.schema_sql, "DuckDB"),
        TextToSqlArm("catalog", model, executor, CATALOG_CONTEXT, "DuckDB"),
        SemanticArm("semantic", structured, fusion, tables_under_test),
        # The control: the same layer with the model taken out entirely. An
        # arm that cannot beat word matching is not paying for its inference.
        LexiconArm("lexicon", fusion, tables_under_test, lexicon=lexicon),
        SemanticArm("semantic-schema", structured, fusion, tables_under_test, constrained=True),
        # The deterministic proposal, corrected by the model: word matching
        # does the mechanical part, the model judges whether that is what was
        # asked, and the schema stops either from inventing anything. Last,
        # because the verdict is read off the final arm and these thresholds
        # were written to judge the layer at its best.
        RepairArm("lexicon+repair", structured, fusion, tables_under_test, lexicon=lexicon),
    ]

    results = []
    for arm in arms:
        print(f"running {arm.name} over {len(questions)} questions...", flush=True)
        results.append(run_arm(arm, tuple(questions), gold, "duckdb", questions.identifiers))
        ids = list(results[-1].grades)
        print(f"  {arm.name}: {results[-1].accuracy(ids)}", flush=True)

    report = build_report(questions, "duckdb", results)
    json_path, md_path = write_report(report, out)
    print()
    print(report.to_markdown())

    # Six questions need a join, which the DSL refuses by design in v1. The
    # subset separates "got it wrong" from "was never able to answer that".
    joins = {q.id for q in questions.by_tag("join")}
    single = [qid for qid in report.ids if qid not in joins]
    subset = subset_report(report, single, "single-table only")
    print()
    print(subset.to_markdown())
    write_report(subset, out)

    # Why each arm lost, appended to the stored report: an accuracy number
    # says an arm was wrong, not whether the layer or the model failed.
    from bench.analyse import to_markdown

    breakdown = to_markdown(json_path)
    print()
    print(breakdown)
    md_path.write_text(md_path.read_text() + "\n\n" + breakdown + "\n", encoding="utf-8")
    print(f"\nWritten to {json_path} and {md_path}")
    fusion.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
