"""The three things being compared.

Every arm answers the same question and returns the same shape, so the runner
does not know which is which. That is what keeps the comparison paired and the
scoring honest.

- **A — raw text-to-SQL.** The model sees only the DDL. The baseline: what you
  get by pointing an LLM at a database.
- **B — Warp catalog.** The model additionally sees the catalog's
  ``x-llm-context``: descriptions, semantic types, relationships, row counts.
- **C — semantic DSL.** The model picks measures, dimensions and transforms
  from the model Fusion exposes, and never writes SQL at all.

Arm C is structurally unable to hallucinate a column — it can only name one
the model offers — which is the hypothesis the whole benchmark exists to test.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from bench.constrain import request_schema, to_dsl, values_by_table
from bench.models import extract_json
from bench.scoring import Answer


class Executor(Protocol):
    """Runs a statement against the database under test."""

    def run(self, sql: str) -> list[tuple[Any, ...]]:
        """Execute and return rows, or raise."""
        ...


class LanguageModel(Protocol):
    """The model under test.

    Deliberately minimal: a benchmark that needs a particular SDK is a
    benchmark nobody re-runs with a different model.
    """

    def complete(self, prompt: str, schema: Mapping[str, Any] | None = None) -> str:
        """One completion for one prompt.

        ``schema``, when given, constrains decoding to a JSON schema. A model
        that cannot do that ignores it and the caller gets the unconstrained
        behaviour, so the argument never makes an adapter unusable.
        """
        ...


class Arm(Protocol):
    """One approach being measured."""

    name: str

    def answer(self, question: Any) -> Answer:
        """Answer one question."""
        ...


#: How many proposals `RepairArm` keeps for after-the-fact comparison. Large
#: enough for any question set, small enough not to be a leak in a long-lived
#: process.
MAX_RECORDED_SEEDS = 500


def _timed(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _extract_sql(text: str) -> str:
    """Pull a statement out of a model reply, fenced or not."""
    stripped = text.strip()
    if "```" in stripped:
        blocks = stripped.split("```")
        for block in blocks[1:]:
            body = block.split("\n", 1)[-1] if block[:3].lower() in ("sql", "sql\n") else block
            body = body.removeprefix("sql").strip()
            if body:
                return body.strip().rstrip(";")
    return stripped.rstrip(";")


class TextToSqlArm:
    """Arms A and B: the model writes SQL, we run it.

    The only difference between them is how much context the prompt carries,
    which is exactly the variable under test — so they share an implementation
    rather than duplicating one with a different prompt buried in it.
    """

    def __init__(
        self,
        name: str,
        model: LanguageModel,
        executor: Executor,
        context: str,
        dialect: str = "",
    ) -> None:
        self.name = name
        self._model = model
        self._executor = executor
        self._context = context
        self._dialect = dialect

    def prompt_for(self, question: Any) -> str:
        dialect = f" for {self._dialect}" if self._dialect else ""
        return (
            f"Write one SQL SELECT statement{dialect} that answers the question.\n"
            f"Return only the statement, with no explanation.\n\n"
            f"{self._context}\n\n"
            f"Question: {question.text}\n"
        )

    def answer(self, question: Any) -> Answer:
        start = time.perf_counter()
        try:
            sql = _extract_sql(self._model.complete(self.prompt_for(question)))
        except Exception as e:
            return Answer(error=f"model failed: {e}", latency_ms=_timed(start))
        try:
            rows = self._executor.run(sql)
        except Exception as e:
            return Answer(sql=sql, error=f"execution failed: {e}", latency_ms=_timed(start))
        return Answer(rows=rows, sql=sql, latency_ms=_timed(start))


class SemanticArm:
    """Arm C: the model picks metrics; Fusion compiles and runs them.

    The model never emits SQL, so a wrong answer here is a wrong *choice* of
    measure or dimension — which is a different and more recoverable failure
    than an invented column.
    """

    def __init__(
        self,
        name: str,
        model: LanguageModel,
        fusion: Any,
        tables: Sequence[str] | str,
        context: str = "",
        constrained: bool = False,
    ) -> None:
        self.name = name
        self._model = model
        self._fusion = fusion
        # Every table, not one: arms A and B are given the whole schema, so
        # restricting this arm to a single table would measure the harness
        # rather than the layer.
        self._tables = [tables] if isinstance(tables, str) else list(tables)
        # Off by default, so the unconstrained arm keeps behaving exactly as
        # it did on the first run and the two remain directly comparable.
        self._constrained = constrained
        self._schema = request_schema(fusion.semantic, self._tables) if constrained else None
        self._context = context or self._describe(with_values=constrained)

    def _describe(self, with_values: bool = False) -> str:
        return describe_tables(self._fusion, self._tables, self._schema if with_values else None)

    def _known_values(self) -> dict[str, list[Any]]:
        return known_values(self._schema)

    def prompt_for(self, question: Any) -> str:
        if self._constrained:
            # No syntax to teach: the schema admits only legal requests, so
            # the prompt carries meaning and nothing else. Spending it on an
            # example of a grammar the model cannot violate would be waste.
            # Deliberately short, and measured rather than assumed. An
            # earlier version spelled out the pragmatics the schema cannot
            # carry — use a dimension for every "per X", a filter for every
            # condition, cumsum for a running total — on the reasoning that
            # arms A and B get all of that free from having seen SQL. It made
            # this arm *worse*: 13/40 correct with these two sentences against
            # 10/40 with the list, and one runtime failure against seven. For
            # a small model an instruction list competes with the question for
            # attention, and the constraint has already removed everything the
            # instructions were protecting against. Constrain the output, do
            # not lecture the model.
            return (
                "Choose the table, metrics and dimensions that answer the "
                "question. Pick the measure the question is actually about.\n\n"
                f"{self._context}\n\n"
                f"Question: {question.text}\n"
            )
        # One worked example. Arms A and B answer in SQL, which the model has
        # seen a great deal of in pretraining; this DSL it has never seen. The
        # example is what makes the comparison about the *information* each arm
        # is given rather than about syntax familiarity.
        return (
            "Answer the question by choosing a table, metrics and dimensions.\n"
            "A metric is written 'measure:aggregation' — NOT a function call.\n"
            "Use '*:count' to count rows. A date dimension may carry a grain, "
            "written 'the_date:month'.\n\n"
            "Example question: monthly revenue per channel, only successful ones\n"
            'Example answer: {"table": "db.sales", "metrics": ["revenue:sum"], '
            '"dimensions": ["kanal", "sale_date:month"], '
            '"filters": [{"column": "durum", "op": "eq", "value": "ok"}]}\n\n'
            f"{self._context}\n\n"
            f"Question: {question.text}\n"
            "Return only the JSON object.\n"
        )

    def answer(self, question: Any) -> Answer:
        start = time.perf_counter()
        try:
            reply = self._model.complete(self.prompt_for(question), self._schema)
            raw = json.loads(extract_json(reply))
        except Exception as e:
            return Answer(error=f"model failed: {e}", latency_ms=_timed(start))

        # Both configurations converge here: a constrained reply carries
        # objects, an unconstrained one strings, and `to_dsl` makes them the
        # same request. One execution path keeps the arms comparable.
        described = json.dumps(raw, sort_keys=True)
        return _execute(self._fusion, self._tables, to_dsl(raw), described, start)


def _execute(
    fusion: Any,
    tables: Sequence[str],
    request: Mapping[str, Any],
    described: str,
    start: float,
) -> Answer:
    """Run a normalised request and grade-ready-ify whatever comes back.

    Shared by the arms that produce a request rather than SQL, so that how the
    request was *chosen* is the only thing that differs between them.
    """
    try:
        table = str(request.get("table") or tables[0])
        if table not in tables:
            # A table it invented: resolve by suffix, else fall back.
            table = next(
                (t for t in tables if t.endswith(table.split(".")[-1])),
                tables[0],
            )
        result = fusion.tools.execute(
            "query_metrics",
            {
                "table": table,
                "metrics": request["metrics"],
                "dimensions": request["dimensions"],
                "filters": request["filters"],
                "order_by": request["order_by"],
                "limit": request["limit"],
            },
        )
    except Exception as e:
        return Answer(
            sql=described,
            error=f"execution failed: {e}",
            latency_ms=_timed(start),
            named=_named_in(request, table),
        )
    if "error" in result:
        return Answer(
            sql=described,
            error=result["error"],
            latency_ms=_timed(start),
            named=_named_in(request, table),
        )
    # The tool returns dict records; the grader compares by position, so the
    # values are read in the result's own column order. `tuple(row)` would
    # hand over the column *names*.
    columns = result["columns"]
    return Answer(
        rows=[tuple(row[name] for name in columns) for row in result["rows"]],
        sql=described,
        latency_ms=_timed(start),
        named=_named_in(request, table),
    )


class RepairArm:
    """The deterministic answer first, then a model asked to correct it.

    The measurement that motivates this: of the questions the lexicon gets
    wrong, half are *one field* away from right — it dropped a condition
    because it had no word for it, or missed a breakdown. A model given that
    request and the question has a far smaller job than one asked to build the
    request from nothing, and it starts from something already valid.

    Decoding stays constrained to the same schema, so the correction cannot
    introduce a name that does not exist. What a model can still do is make a
    right answer wrong, which is why this arm reports that separately: for a
    copilot, breaking a correct proposal is worse than leaving a wrong one.
    """

    def __init__(
        self,
        name: str,
        model: LanguageModel,
        fusion: Any,
        tables: Sequence[str] | str,
        lexicon: Mapping[str, Any] | None = None,
        fall_back: bool = False,
    ) -> None:
        """
        Args:
            fall_back: Re-run the lexicon's proposal when the correction fails
                to execute. Off by default so the measured arm reports what the
                model actually did; on is the right setting for a product,
                where a correction that does not run must never replace a
                proposal that does.
        """
        from bench.lexicon import LexiconPlanner

        self.name = name
        self._model = model
        self._fusion = fusion
        self._tables = [tables] if isinstance(tables, str) else list(tables)
        self._fall_back = fall_back
        # Built first, then read from three times over: the planner's
        # vocabulary, the prompt's context and the decoding constraint all
        # come out of it. Discovering each separately ran the value queries
        # three times — 42 DSL round trips over 14 dimensions.
        self._schema = request_schema(fusion.semantic, self._tables)
        self._planner = LexiconPlanner(
            fusion.semantic,
            self._tables,
            synonyms=(lexicon or {}).get("columns"),
            value_synonyms=(lexicon or {}).get("values"),
            values=values_by_table(self._schema),
        )
        self._context = describe_tables(fusion, self._tables, self._schema)
        #: What the lexicon proposed, per question id, so a caller can tell a
        #: repair that helped from one that did damage. Bounded, because this
        #: is the shape a served copilot would take and an unbounded store of
        #: every request ever planned would hold filter values read out of
        #: user data. Questions without an id are not recorded at all: they
        #: would all collide on one key and report the last seed as every
        #: question's.
        self.seeds: dict[str, dict[str, Any]] = {}

    def prompt_for(self, question: Any, seed: Mapping[str, Any]) -> str:
        return (
            "A first attempt at answering the question is given below. Correct "
            "it if it does not answer the question, or repeat it unchanged if "
            "it does.\n\n"
            f"{self._context}\n\n"
            f"Question: {question.text}\n"
            f"First attempt: {json.dumps(seed, sort_keys=True, ensure_ascii=False)}\n"
        )

    def answer(self, question: Any) -> Answer:
        start = time.perf_counter()
        try:
            seed = self._planner.plan(question.text)
        except Exception as e:
            return Answer(error=f"planning failed: {e}", latency_ms=_timed(start))
        question_id = getattr(question, "id", "")
        if question_id:
            if len(self.seeds) >= MAX_RECORDED_SEEDS:
                self.seeds.pop(next(iter(self.seeds)))
            self.seeds[question_id] = seed
        try:
            reply = self._model.complete(self.prompt_for(question, seed), self._schema)
            raw = json.loads(extract_json(reply))
        except Exception as e:
            # Reported, not swallowed. Quietly running the proposal instead
            # would turn this arm into the lexicon arm the moment the model
            # became unreachable, and the run would show no sign of it.
            if not self._fall_back:
                return Answer(error=f"model failed: {e}", latency_ms=_timed(start))
            return self._keep_the_proposal(seed, start)

        answer = _execute(
            self._fusion, self._tables, to_dsl(raw), json.dumps(raw, sort_keys=True), start
        )
        if answer.error and self._fall_back and raw != seed:
            return self._keep_the_proposal(seed, start)
        return answer

    def _keep_the_proposal(self, seed: Mapping[str, Any], start: float) -> Answer:
        """Run the lexicon's proposal, and say that is what happened."""
        answer = _execute(
            self._fusion, self._tables, to_dsl(seed), json.dumps(seed, sort_keys=True), start
        )
        return replace(answer, fell_back=True)


class LexiconArm:
    """Arm E: the same layer, chosen by matching words instead of by a model.

    The control the comparison otherwise lacks. Every other arm spends a
    language model on the question; this one spends a lexicon. An arm that
    cannot beat it is not earning its inference cost, and on-premise this is
    the only configuration that answers the same way twice by construction.
    """

    def __init__(
        self,
        name: str,
        fusion: Any,
        tables: Sequence[str] | str,
        lexicon: Mapping[str, Any] | None = None,
    ) -> None:
        """
        Args:
            lexicon: A ``{"columns": …, "values": …}`` vocabulary, as
                ``bench.lexicon_gen`` produces. ``None`` uses the hand-written
                one, which is what separates "a model wrote the lexicon" from
                "a person did" as a measurable difference rather than a claim.
        """
        from bench.lexicon import LexiconPlanner

        self.name = name
        self._fusion = fusion
        self._tables = [tables] if isinstance(tables, str) else list(tables)
        self._planner = LexiconPlanner(
            fusion.semantic,
            self._tables,
            synonyms=(lexicon or {}).get("columns"),
            value_synonyms=(lexicon or {}).get("values"),
        )

    def answer(self, question: Any) -> Answer:
        start = time.perf_counter()
        try:
            raw = self._planner.plan(question.text)
        except Exception as e:
            return Answer(error=f"planning failed: {e}", latency_ms=_timed(start))
        return _execute(
            self._fusion, self._tables, to_dsl(raw), json.dumps(raw, sort_keys=True), start
        )


def known_values(schema: Mapping[str, Any] | None) -> dict[str, list[Any]]:
    """The value enums a request schema carries, per column.

    Read back off the schema rather than rediscovered, so the prompt and the
    constraint cannot drift — and so the discovery queries run once.
    """
    branches = (schema or {}).get("oneOf") or [schema or {}]
    found: dict[str, list[Any]] = {}
    for branch in branches:
        filters = branch.get("properties", {}).get("filters", {})
        for option in filters.get("items", {}).get("oneOf", []):
            properties = option.get("properties", {})
            values = properties.get("value", {}).get("enum")
            if values:
                found[properties["column"]["const"]] = values
    return found


def describe_tables(
    fusion: Any, tables: Sequence[str], schema: Mapping[str, Any] | None = None
) -> str:
    """What an arm tells the model about the tables it may use.

    Passing ``schema`` spells out each categorical dimension's stored values.
    The catalog arm's context already names them — ``durum: basarili | iptal``
    — so withholding them would compare contexts rather than layers.
    """
    if not tables:
        raise ValueError("describe_tables needs at least one table to describe.")
    known = known_values(schema) if schema else {}
    blocks = []
    for table in tables:
        described = fusion.tools.list_metrics(table)
        measures = ", ".join(m["name"] for m in described.get("measures", []))
        dimensions = ", ".join(
            d["name"]
            + (f" ({' | '.join(str(v) for v in known[d['name']])})" if d["name"] in known else "")
            for d in described.get("dimensions", [])
        )
        blocks.append(f"Table {table}\n  measures: {measures}\n  dimensions: {dimensions}")
    transforms = ", ".join(t["name"] for t in fusion.tools.list_metrics(tables[0])["transforms"])
    return (
        "\n".join(blocks)
        + f"\nTransforms (optional, wrap a metric): {transforms}"
        + "\nAggregations: sum, avg, min, max, count, median, count_distinct"
    )


def _named_in(request: Mapping[str, Any], table: str) -> tuple[str, ...]:
    """The schema names a semantic request referenced.

    The measure and dimension names, the filter columns and the table — not
    the aggregations, transforms or JSON keys, which are the language rather
    than the schema.
    """
    names: set[str] = {table.split(".")[-1]}
    for metric in request.get("metrics") or []:
        # "change_pct(cumsum(revenue:sum))" -> revenue
        text = str(metric)
        measure = text.rsplit("(", 1)[-1].split(":", 1)[0].strip(" )")
        if measure and measure != "*":
            names.add(measure)
    for dimension in request.get("dimensions") or []:
        names.add(str(dimension).split(":", 1)[0])
    for condition in request.get("filters") or []:
        if isinstance(condition, Mapping) and condition.get("column"):
            names.add(str(condition["column"]))
    return tuple(sorted(names))


class ScriptedModel:
    """A model that replays fixed answers, for testing the harness itself.

    The harness has to be trustworthy before the numbers it produces mean
    anything, and that cannot be established with a real model in the loop.
    """

    def __init__(self, replies: dict[str, str] | Sequence[str]) -> None:
        self._by_prompt = replies if isinstance(replies, dict) else None
        self._queue = list(replies) if not isinstance(replies, dict) else []
        self.prompts: list[str] = []
        #: The schema each call was constrained by, so a test can assert that
        #: an arm actually constrained decoding rather than merely intending to.
        self.schemas: list[Mapping[str, Any] | None] = []

    def complete(self, prompt: str, schema: Mapping[str, Any] | None = None) -> str:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self._by_prompt is not None:
            for needle, reply in self._by_prompt.items():
                if needle in prompt:
                    return reply
            raise KeyError("ScriptedModel has no reply for this prompt")
        if not self._queue:
            raise KeyError("ScriptedModel ran out of replies")
        return self._queue.pop(0)
