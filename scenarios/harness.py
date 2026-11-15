"""Run scenarios through a real FusionApp and check every answer.

A scenario is a function over a :class:`Ctx`. Each call on the context is one
*step*: it runs something through Fusion, runs the same thing against the
oracle (the source data, read directly), and records whether the two agree,
how long each took and how many rows had to leave the source for it.

A failed step does not stop the scenario: the point of a stress run is the
whole list of what broke, not the first item on it.
"""

from __future__ import annotations

import math
import resource
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fusion import FusionApp, Settings, build_app
from fusion.domain.errors import FusionError, GuardrailViolation, QueryError
from fusion.domain.models import QueryResult
from fusion.domain.policy import QueryPlan
from scenarios.source import DuckSource
from scenarios.synth import ALL_DATABASES, World, open_world

# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budgets:
    """Row budgets as they would be at scale 1.0; scaled with the data.

    Scaling them is what keeps a scenario meaningful at any size: a table that
    is "too big to load" at full scale is still too big in a smoke run, so the
    planner takes the same branch.
    """

    full: int = 600_000
    slice: int = 600_000
    total: int = 2_400_000
    keys: int = 50_000

    def settings(self, scale: float) -> dict[str, int]:
        return {
            "full_load_max_rows": max(50, int(self.full * scale)),
            "slice_max_rows": max(50, int(self.slice * scale)),
            "slice_budget_rows": max(200, int(self.total * scale)),
            "semi_join_max_keys": max(20, int(self.keys * scale)),
        }


UNLIMITED = Budgets(full=10**12, slice=10**12, total=10**13, keys=10**9)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    sector: str
    title: str
    story: str
    fn: Callable[[Ctx], None]
    sources: tuple[str, ...]
    capabilities: tuple[str, ...] = ()
    budgets: Budgets = Budgets()
    settings: Mapping[str, Any] = field(default_factory=dict)
    #: ``inproc`` reads the synthesized files directly; ``warp`` goes through
    #: real databases behind Warp. A scenario that changes source data runs
    #: in-process only.
    modes: tuple[str, ...] = ("inproc", "warp")
    #: Sources connected without raw SQL although the database offers it, so
    #: a question that would be pushed down exercises the slice path instead.
    no_pushdown: tuple[str, ...] = ()


REGISTRY: dict[str, Scenario] = {}


def scenario(
    id: str,
    *,
    sector: str,
    title: str,
    sources: Sequence[str],
    capabilities: Sequence[str] = (),
    budgets: Budgets | None = None,
    settings: Mapping[str, Any] | None = None,
    modes: Sequence[str] = ("inproc", "warp"),
    no_pushdown: Sequence[str] = (),
) -> Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]:
    """Register a scenario; its docstring is the story shown in the report."""

    def register(fn: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        if id in REGISTRY:
            raise ValueError(f"Duplicate scenario id {id}")
        REGISTRY[id] = Scenario(
            id=id,
            sector=sector,
            title=title,
            story=" ".join((fn.__doc__ or "").split()),
            fn=fn,
            sources=tuple(sources),
            capabilities=tuple(capabilities),
            budgets=budgets or Budgets(),
            settings=dict(settings or {}),
            modes=tuple(modes),
            no_pushdown=tuple(no_pushdown),
        )
        return fn

    return register


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Step:
    kind: str
    label: str
    ok: bool
    detail: str = ""
    ms: float = 0.0
    oracle_ms: float = 0.0
    rows: int | None = None
    #: Rows that left the source for this step (in-process runs only).
    moved: int | None = None
    actions: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "ok": self.ok,
            "detail": self.detail,
            "ms": round(self.ms, 2),
            "oracle_ms": round(self.oracle_ms, 2),
            "rows": self.rows,
            "moved": self.moved,
            "actions": self.actions,
        }


@dataclass(slots=True)
class ScenarioResult:
    scenario: Scenario
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    peak_rss_mb: float = 0.0
    skipped: str = ""
    source_rows: int = 0
    moved: int = 0

    @property
    def passed(self) -> bool:
        return not self.error and not self.skipped and all(s.ok for s in self.steps)

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario.id,
            "sector": self.scenario.sector,
            "title": self.scenario.title,
            "story": self.scenario.story,
            "capabilities": list(self.scenario.capabilities),
            "sources": list(self.scenario.sources),
            "passed": self.passed,
            "skipped": self.skipped,
            "error": self.error,
            "seconds": round(self.seconds, 3),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "source_rows": self.source_rows,
            "moved": self.moved,
            "steps": [s.as_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# Comparing a result to the oracle
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A cell in the form both sides can be compared in."""
    if value is None or isinstance(value, bool | int | str | Decimal):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        # The store keeps zoned timestamps as UTC wall-clock time.
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, list | tuple):
        return tuple(_plain(v) for v in value)
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _plain(v)) for k, v in value.items()))
    return str(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _sort_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
    key = []
    for cell in row:
        if cell is None:
            key.append((0, ""))
        elif _is_number(cell):
            number = float(cell)
            key.append((1, "nan" if math.isnan(number) else f"{number:+.6e}"))
        else:
            key.append((2, str(cell)))
    return tuple(key)


def _json_text_equal(text: str, value: Any) -> bool:
    """Whether ``text`` is how a JSON response would spell ``value``.

    Warp's raw-query endpoint answers in JSON with no column types: a DECIMAL
    arrives as ``"1764687.92"`` and a timestamp as ``"2026-08-03T00:00:00Z"``.
    """
    try:
        if isinstance(value, Decimal):
            return Decimal(text) == value
        if isinstance(value, datetime | date):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            if isinstance(value, datetime):
                return parsed == value
            return parsed.date() == value and parsed.time() == datetime.min.time()
    except (ArithmeticError, ValueError):
        return False
    return False


def _cells_equal(left: Any, right: Any, tolerance: float, from_json: bool = False) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if from_json and isinstance(left, str) and not isinstance(right, str):
        return _json_text_equal(left, right)
    if _is_number(left) and _is_number(right):
        if (isinstance(left, int) and isinstance(right, int)) or tolerance == 0:
            return bool(left == right)
        a, b = float(left), float(right)
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _cells_equal(a, b, tolerance, from_json) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, datetime) != isinstance(right, datetime):
        # A date against a midnight timestamp of the same day is the same value.
        midnight = ("", "00:00:00")
        return (
            str(left)[:10] == str(right)[:10]
            and str(left)[11:] in midnight
            and str(right)[11:] in midnight
        )
    return bool(left == right)


def compare_rows(
    got: Sequence[Sequence[Any]],
    want: Sequence[Sequence[Any]],
    *,
    ordered: bool = False,
    tolerance: float = 1e-9,
    from_json: bool = False,
) -> str:
    """Empty when the two results agree, else what differs.

    ``from_json`` reads a text cell on the Fusion side as the JSON spelling of
    the oracle's DECIMAL, date or timestamp (see ``_json_text_equal``).
    """
    if len(got) != len(want):
        return f"row count differs: fusion {len(got):,} vs oracle {len(want):,}"
    left = [tuple(_plain(c) for c in row) for row in got]
    right = [tuple(_plain(c) for c in row) for row in want]
    if not ordered:
        left.sort(key=_sort_key)
        right.sort(key=_sort_key)
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        if len(a) != len(b):
            return f"column count differs: fusion {len(a)} vs oracle {len(b)}"
        if not all(_cells_equal(x, y, tolerance, from_json) for x, y in zip(a, b, strict=True)):
            return f"row {index} differs: fusion {a!r} vs oracle {b!r}"
    return ""


# ---------------------------------------------------------------------------
# Scheduler and clock the scenarios can drive
# ---------------------------------------------------------------------------


class _Job:
    def __init__(self, interval_s: float, fn: Callable[[], None], name: str) -> None:
        self.interval_s = interval_s
        self.fn = fn
        self.name = name
        self.cancelled = False
        self.runs = 0

    def cancel(self) -> None:
        self.cancelled = True


class ManualScheduler:
    """Jobs run when the scenario says the time has passed, not on a timer."""

    def __init__(self) -> None:
        self.jobs: list[_Job] = []

    def every(self, interval_s: float, fn: Callable[[], None], *, name: str) -> _Job:
        job = _Job(interval_s, fn, name)
        self.jobs.append(job)
        return job

    def cancel_all(self) -> None:
        for job in self.jobs:
            job.cancel()

    def tick(self) -> int:
        ran = 0
        for job in list(self.jobs):
            if not job.cancelled:
                job.runs += 1
                job.fn()
                ran += 1
        return ran


class Clock:
    """Wall-clock time the scenario can push forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


# ---------------------------------------------------------------------------
# The context a scenario runs in
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunConfig:
    scale: float
    root: Path
    mode: str = "inproc"
    #: Warp base URL per database (``*`` is the default).
    warp_urls: Mapping[str, str] = field(default_factory=dict)
    warp_api_key: str = ""
    memory_limit: str = "4GB"
    threads: int = 4
    latency_ms: float = 0.0


class Ctx:
    """What a scenario works with."""

    def __init__(
        self,
        scenario: Scenario,
        app: FusionApp,
        world: World,
        config: RunConfig,
        sources: dict[str, DuckSource],
        scheduler: ManualScheduler,
        clock: Clock,
        connect: Callable[[str, Mapping[str, Any]], None],
    ) -> None:
        self.scenario = scenario
        self.app = app
        self.world = world
        self.config = config
        self.scheduler = scheduler
        self.clock = clock
        self.steps: list[Step] = []
        self._sources = sources
        self._lock = threading.Lock()
        self._scratch: list[str] = []
        self._connect = connect

    # -- facts --------------------------------------------------------------

    @property
    def scale(self) -> float:
        return self.config.scale

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def inproc(self) -> bool:
        return self.config.mode == "inproc"

    def rows(self, table: str) -> int:
        """Source-side size of ``database.table``."""
        return self.world.rows(table)

    def source(self, name: str) -> DuckSource | None:
        """The in-process source behind ``name`` (None on a Warp run)."""
        return self._sources.get(name)

    def moved(self) -> int:
        """Rows that have left the sources so far (in-process runs)."""
        return sum(s.stats.rows_sent for s in self._sources.values())

    def held(self) -> int:
        """Rows sitting in the analytics store right now."""
        return sum(s.row_count for s in self.app.catalog.all_slices())

    def budget(self, name: str) -> int:
        return int(getattr(self.app.settings, name))

    # -- recording ----------------------------------------------------------

    def _add(self, step: Step) -> Step:
        with self._lock:
            self.steps.append(step)
        return step

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        """Record a plain assertion."""
        self._add(Step("check", label, bool(ok), "" if ok else detail or "condition was false"))
        return bool(ok)

    def note(self, label: str, detail: str = "") -> None:
        """Record an observation that is neither right nor wrong."""
        self._add(Step("note", label, True, detail))

    # -- oracle -------------------------------------------------------------

    def oracle(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run ``sql`` against the source data itself."""
        cursor = self.world.cursor()
        return cursor.execute(sql, list(params) if params else None).fetchall()

    def scalar(self, sql: str) -> Any:
        return self.oracle(sql)[0][0]

    # -- planning -----------------------------------------------------------

    def plan(self, sql: str) -> QueryPlan:
        return self.app.query.planner.plan_query(sql, estimator=self.app.sources.estimate_slice)

    def actions(self, sql: str) -> dict[str, str]:
        """What the planner would do about each table ``sql`` reads, right now."""
        return {t.ref.full_name: t.action for t in self.plan(sql).targets}

    def pushed(self, sql: str, table: str) -> dict[str, Any]:
        """What of ``sql`` reaches the source for ``table``: columns, conditions, limit."""
        for target in self.plan(sql).targets:
            if target.ref.full_name == table:
                return {
                    "action": target.action,
                    "columns": None if target.spec.columns is None else set(target.spec.columns),
                    "predicates": {(p.column, p.op) for p in target.spec.predicates},
                    "limit": target.spec.limit,
                }
        return {"action": None, "columns": None, "predicates": set(), "limit": None}

    # -- steps --------------------------------------------------------------

    def sql(
        self,
        sql: str,
        label: str = "",
        *,
        ordered: bool = False,
        actions: Mapping[str, str | tuple[str, ...]] | None = None,
        pushdown: bool | None = None,
        use_cache: bool = True,
        params: Sequence[Any] | None = None,
        oracle_sql: str | None = None,
        compare: bool = True,
        tolerance: float = 1e-9,
        max_moved: int | None = None,
        from_cache: bool | None = None,
    ) -> QueryResult | None:
        """Run a query through Fusion and check it against the oracle.

        ``actions`` asserts what the planner decides per table before the
        query runs (``load_slice``, ``semi_join``, ``reuse``, ...).
        ``pushdown`` asserts that the source answered it whole (or did not).
        ``max_moved`` caps the rows that may leave the sources for it.
        """
        label = label or " ".join(sql.split())[:70]
        step = Step("sql", label, True)
        problems: list[str] = []
        try:
            planned = self.actions(sql) if (actions is not None or self.inproc) else {}
        except FusionError as e:
            planned = {}
            problems.append(f"planning failed: {e}")
        step.actions = planned
        for table, wanted in (actions or {}).items():
            allowed = (wanted,) if isinstance(wanted, str) else tuple(wanted)
            if planned.get(table) not in allowed:
                problems.append(
                    f"planner chose {planned.get(table)!r} for {table}, expected "
                    f"{' or '.join(allowed)}"
                )

        moved_before = self.moved()
        held_before = self.held()
        pushes_before = self._pushdowns()
        result: QueryResult | None = None
        started = time.perf_counter()
        try:
            result = self.app.query.sql(sql, use_cache=use_cache, params=params)
        except Exception as e:  # the step fails; the scenario goes on
            problems.append(f"{type(e).__name__}: {_first_line(e)}")
        step.ms = (time.perf_counter() - started) * 1000
        if self.inproc:
            step.moved = self.moved() - moved_before

        if result is not None:
            step.rows = result.row_count
            if compare:
                started = time.perf_counter()
                try:
                    want = self.oracle(oracle_sql or sql, params)
                except Exception as e:
                    problems.append(f"oracle failed: {_first_line(e)}")
                else:
                    step.oracle_ms = (time.perf_counter() - started) * 1000
                    difference = compare_rows(
                        result.rows,
                        want,
                        ordered=ordered,
                        tolerance=tolerance,
                        from_json=pushdown is True and not self.inproc,
                    )
                    if difference:
                        problems.append(difference)
            if from_cache is not None and result.from_cache is not from_cache:
                problems.append(f"from_cache was {result.from_cache}, expected {from_cache}")
            if pushdown is not None:
                if self.inproc:
                    pushed = self._pushdowns() > pushes_before
                elif not pushdown:
                    # Through a real Warp a local answer and a pushed one both load
                    # nothing new, so "ran locally" cannot be told apart from outside.
                    pushed = False
                else:
                    pushed = self.held() == held_before and not result.from_cache
                if pushed is not pushdown:
                    problems.append(
                        "expected the source to answer this itself"
                        if pushdown
                        else "expected this to run locally, but it was pushed down"
                    )
            if max_moved is not None and step.moved is not None and step.moved > max_moved:
                problems.append(
                    f"{step.moved:,} rows left the source, expected at most {max_moved:,}"
                )

        step.ok = not problems
        step.detail = "; ".join(problems)
        self._add(step)
        return result

    def refused(
        self, sql: str, label: str = "", *, contains: Sequence[str] = ("Refusing to load",)
    ) -> str:
        """Expect the planner to refuse ``sql`` and say why; returns the message."""
        label = label or "refused: " + " ".join(sql.split())[:60]
        moved_before = self.moved()
        started = time.perf_counter()
        try:
            self.app.query.sql(sql, use_cache=False)
        except QueryError as e:
            message = str(e)
            missing = [text for text in contains if text not in message]
            moved = self.moved() - moved_before if self.inproc else None
            problems = [f"refusal does not mention {text!r}" for text in missing]
            if moved:
                problems.append(f"{moved:,} rows were fetched before refusing")
            self._add(
                Step(
                    "refusal",
                    label,
                    not problems,
                    "; ".join(problems) or message.splitlines()[0],
                    ms=(time.perf_counter() - started) * 1000,
                    moved=moved,
                )
            )
            return message
        except Exception as e:
            self._add(Step("refusal", label, False, f"{type(e).__name__}: {_first_line(e)}"))
            return ""
        self._add(Step("refusal", label, False, "the query ran; a refusal was expected"))
        return ""

    def limit(self, sql: str, label: str, *, ordered: bool = False) -> str:
        """Record where the engine draws a line, without calling it a failure.

        Some honest questions are refused by design (a self-join or a window
        over a table too big to load). The step passes when Fusion either
        refuses cleanly or answers correctly; a wrong answer or a crash fails.
        Returns ``"answered"``, ``"refused"`` or ``"failed"``.
        """
        started = time.perf_counter()
        try:
            result = self.app.query.sql(sql, use_cache=False)
        except QueryError as e:
            self._add(
                Step(
                    "limit",
                    label,
                    True,
                    "refused: " + _first_line(e),
                    ms=(time.perf_counter() - started) * 1000,
                )
            )
            return "refused"
        except Exception as e:
            self._add(Step("limit", label, False, f"raw {type(e).__name__}: {_first_line(e)}"))
            return "failed"
        ms = (time.perf_counter() - started) * 1000
        difference = compare_rows(result.rows, self.oracle(sql), ordered=ordered)
        self._add(
            Step(
                "limit",
                label,
                not difference,
                difference or "answered",
                ms=ms,
                rows=result.row_count,
            )
        )
        return "failed" if difference else "answered"

    def blocked(self, sql: str, label: str = "") -> None:
        """Expect the guardrails to stop ``sql`` before it reaches the store."""
        label = label or "blocked: " + " ".join(sql.split())[:60]
        moved_before = self.moved()
        try:
            self.app.query.sql(sql, use_cache=False)
        except GuardrailViolation as e:
            moved = self.moved() - moved_before if self.inproc else None
            self._add(
                Step(
                    "guardrail",
                    label,
                    not moved,
                    "rows were fetched before the block" if moved else _first_line(e),
                )
            )
            return
        except Exception as e:
            moved = self.moved() - moved_before if self.inproc else None
            fetched = f" after {moved:,} rows were fetched" if moved else ""
            self._add(
                Step(
                    "guardrail",
                    label,
                    False,
                    f"passed the guardrails and was stopped by the store{fetched}: "
                    f"{type(e).__name__}: {_first_line(e)}",
                    moved=moved,
                )
            )
            return
        self._add(Step("guardrail", label, False, "the statement ran"))

    def fails(
        self, sql: str, label: str, *, contains: str = "", params: Sequence[Any] | None = None
    ) -> str:
        """Expect a clean ``FusionError`` (not a crash, not an answer)."""
        try:
            self.app.query.sql(sql, use_cache=False, params=params)
        except FusionError as e:
            ok = contains.lower() in str(e).lower()
            self._add(
                Step(
                    "error",
                    label,
                    ok,
                    _first_line(e) if ok else f"missing {contains!r}: {_first_line(e)}",
                )
            )
            return str(e)
        except Exception as e:
            self._add(Step("error", label, False, f"raw {type(e).__name__}: {_first_line(e)}"))
            return ""
        self._add(Step("error", label, False, "the query ran; an error was expected"))
        return ""

    def tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        label: str = "",
        *,
        expect_error: str | None = None,
    ) -> dict[str, Any]:
        """Call one of the LLM tools exactly as an agent would."""
        label = label or f"{name}({_brief(arguments)})"
        moved_before = self.moved()
        started = time.perf_counter()
        try:
            result = self.app.tools.execute(name, dict(arguments or {}))
        except Exception as e:  # execute() promises never to raise
            self._add(Step("tool", label, False, f"raised {type(e).__name__}: {_first_line(e)}"))
            return {}
        step = Step("tool", label, True, ms=(time.perf_counter() - started) * 1000)
        if self.inproc:
            step.moved = self.moved() - moved_before
        error = result.get("error") if isinstance(result, dict) else None
        if expect_error is None:
            if error:
                step.ok, step.detail = False, str(error).splitlines()[0]
        elif not error:
            step.ok, step.detail = False, "the tool answered; an error was expected"
        elif expect_error.lower() not in str(error).lower():
            step.ok = False
            step.detail = f"error lacks {expect_error!r}: {str(error).splitlines()[0]}"
        else:
            step.detail = str(error).splitlines()[0]
        if isinstance(result, dict) and "row_count" in result:
            step.rows = result["row_count"]
        self._add(step)
        return result if isinstance(result, dict) else {}

    def tool_rows(
        self,
        name: str,
        arguments: Mapping[str, Any],
        oracle_sql: str,
        label: str = "",
        *,
        ordered: bool = False,
        tolerance: float = 1e-9,
    ) -> dict[str, Any]:
        """Call a tool and check the rows it returns against the oracle."""
        result = self.tool(name, arguments, label)
        if "rows" not in result:
            return result
        got = [
            tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
            for row in result["rows"]
        ]
        difference = compare_rows(
            got, self.oracle(oracle_sql), ordered=ordered, tolerance=tolerance
        )
        self.check(f"{label or name}: rows match the oracle", not difference, difference)
        return result

    def _pushdowns(self) -> int:
        return sum(s.stats.pushdowns for s in self._sources.values())

    # -- scratch sources (for scenarios that change source data) --------------

    def scratch(
        self,
        name: str,
        tables: Mapping[str, str],
        *,
        pushdown: bool = False,
        config: Mapping[str, Any] | None = None,
    ) -> DuckSource:
        """Create a writable source database for this scenario alone.

        ``tables`` maps a table name to the SELECT that fills it. The catalog
        is detached again when the scenario ends, so nothing it changes can
        reach another scenario.
        """
        if not self.inproc:
            raise RuntimeError("scratch sources exist only on in-process runs")
        conn = self.world.conn
        conn.execute(f"ATTACH ':memory:' AS \"{name}\"")
        self._scratch.append(name)
        for table, select in tables.items():
            conn.execute(f'CREATE TABLE "{name}".main."{table}" AS {select}')
            count = conn.execute(f'SELECT count(*) FROM "{name}".main."{table}"').fetchone()
            self.world.sizes[f"{name}.{table}"] = int(count[0]) if count else 0
        self._connect(
            name, {"type": "synthetic", "catalog": name, "pushdown": pushdown, **(config or {})}
        )
        return self._sources[name]

    def attach(self, name: str, catalog: str, **config: Any) -> DuckSource:
        """Connect another source over a database already in the world.

        ``config`` reaches the in-process source: ``pushdown``, ``latency_ms``,
        ``export_max_rows`` (the source's own cap on one read).
        """
        if not self.inproc:
            raise RuntimeError("attach exists only on in-process runs")
        self._connect(name, {"type": "synthetic", "catalog": catalog, **config})
        for key, rows in list(self.world.sizes.items()):
            if key.startswith(f"{catalog}."):
                self.world.sizes[f"{name}.{key.split('.', 1)[1]}"] = rows
        return self._sources[name]

    def mutate(self, sql: str) -> None:
        """Change source data (a scratch source's, by construction)."""
        self.world.conn.cursor().execute(sql)

    def cleanup(self) -> None:
        for name in self._scratch:
            try:
                self.world.conn.execute(f'DETACH "{name}"')
            except Exception:  # pragma: no cover - best effort
                pass
            for key in [k for k in self.world.sizes if k.startswith(f"{name}.")]:
                del self.world.sizes[key]


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0][:300] if text else type(error).__name__


def _brief(arguments: Mapping[str, Any] | None) -> str:
    if not arguments:
        return ""
    text = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _settings(scenario: Scenario, config: RunConfig) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "memory_limit": config.memory_limit,
        "threads": config.threads,
        "warp_api_key": config.warp_api_key,
        **scenario.budgets.settings(config.scale),
    }
    for key, value in scenario.settings.items():
        values[key] = value(config.scale) if callable(value) else value
    return Settings(**values)


def run_scenario(scenario: Scenario, world: World, config: RunConfig) -> ScenarioResult:
    """Run one scenario against a fresh FusionApp."""
    result = ScenarioResult(scenario=scenario)
    if config.mode not in scenario.modes:
        result.skipped = f"runs only in {', '.join(scenario.modes)} mode"
        return result

    sources: dict[str, DuckSource] = {}

    def factory(name: str, source_config: Mapping[str, Any]) -> Any:
        if source_config.get("type") == "warp":
            from fusion.adapters.outbound.registry import default_registry

            return default_registry(settings.warp_http_defaults()).create(name, source_config)
        source = DuckSource(
            name,
            world.conn,
            str(source_config.get("catalog", name)),
            pushdown=bool(source_config.get("pushdown", False)),
            latency_ms=float(source_config.get("latency_ms", config.latency_ms)),
            export_max_rows=source_config.get("export_max_rows"),
        )
        sources[name] = source
        return source

    settings = _settings(scenario, config)
    scheduler = ManualScheduler()
    clock = Clock()
    app = build_app(settings, source_factory=factory, scheduler=scheduler, clock=clock)

    def connect(name: str, source_config: Mapping[str, Any]) -> None:
        app.sources.connect(name, source_config)

    ctx = Ctx(scenario, app, world, config, sources, scheduler, clock, connect)
    started = time.perf_counter()
    try:
        for name in scenario.sources:
            database = world.databases[name]
            pushdown = database.pushdown and name not in scenario.no_pushdown
            if config.mode == "warp":
                # Raw SQL is a property of the Warp instance, so the choice of
                # URL is the choice of pushdown.
                url = (config.warp_urls.get(name) if pushdown else None) or config.warp_urls["*"]
                connect(name, {"type": "warp", "base_url": url, "database": name})
            else:
                connect(name, {"type": "synthetic", "pushdown": pushdown})
            result.source_rows += sum(world.rows(f"{name}.{t.name}") for t in database.tables)
        scenario.fn(ctx)
    except Exception as e:
        result.error = f"{type(e).__name__}: {_first_line(e)}"
        result.error += "\n" + "".join(traceback.format_exception(e)[-4:])
    finally:
        result.seconds = time.perf_counter() - started
        result.moved = ctx.moved()
        try:
            app.close()
        finally:
            ctx.cleanup()
    result.steps = ctx.steps
    result.peak_rss_mb = _peak_rss_mb()
    return result


def select(
    ids: Iterable[str] = (), sectors: Iterable[str] = (), capability: str = ""
) -> list[Scenario]:
    """The registered scenarios matching the filters, in registration order."""
    load_cases()
    wanted_ids = {i.upper() for i in ids}
    wanted_sectors = {s.lower() for s in sectors}
    chosen = []
    for scenario_ in REGISTRY.values():
        if wanted_ids and scenario_.id.upper() not in wanted_ids:
            continue
        if wanted_sectors and scenario_.sector.lower() not in wanted_sectors:
            continue
        if capability and capability not in scenario_.capabilities:
            continue
        chosen.append(scenario_)
    return chosen


def load_cases() -> None:
    """Import every case module so its scenarios register."""
    import scenarios.cases  # noqa: F401


def run(
    scenarios_: Sequence[Scenario],
    config: RunConfig,
    *,
    say: Callable[[str], None] = print,
) -> list[ScenarioResult]:
    """Run ``scenarios_`` one after another against one world."""
    needed = {name for s in scenarios_ for name in s.sources}
    world = open_world(ALL_DATABASES, config.scale, config.root, names=needed)
    results = []
    try:
        for item in scenarios_:
            result = run_scenario(item, world, config)
            results.append(result)
            say(summary_line(result))
            for step in result.failures:
                say(f"      ✗ {step.label}: {step.detail}")
            if result.error:
                say("      ! " + result.error.replace("\n", "\n        "))
    finally:
        world.close()
    return results


def summary_line(result: ScenarioResult) -> str:
    scenario_ = result.scenario
    if result.skipped:
        return f"  SKIP {scenario_.id:5s} {scenario_.title} ({result.skipped})"
    mark = "PASS" if result.passed else "FAIL"
    ok = sum(1 for s in result.steps if s.ok)
    moved = f", {result.moved:,} rows moved" if result.moved else ""
    return (
        f"  {mark} {scenario_.id:5s} {scenario_.title} — {ok}/{len(result.steps)} steps, "
        f"{result.seconds:.2f}s{moved}"
    )
