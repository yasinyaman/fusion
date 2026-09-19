"""SemanticService: answering a metric question without anyone writing SQL.

The flow is deliberately a chain of small, separately testable steps:

    parse metrics -> resolve the model -> build and validate the query
      -> derive the shape -> materialize what it needs -> compile -> run

Nothing here imports sqlglot; the compiler behind the ``SemanticCompiler`` port
does that. What this service owns is the order of the steps and the decision to
execute locally.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from fusion.application.query import QueryService
from fusion.domain.catalog import SchemaCatalog
from fusion.domain.errors import QueryError, SchemaError
from fusion.domain.identifiers import MAX_RESULT_ROWS, validate_identifier
from fusion.domain.measures import SemanticModel, infer_model
from fusion.domain.metric_dsl import parse_metric
from fusion.domain.models import QueryResult, TableRef, coerce_ref
from fusion.domain.semantic_query import SemanticQuery, parse_dimension
from fusion.domain.slices import PREDICATE_OPS, Predicate, PredicateOp
from fusion.domain.transforms import TRANSFORMS
from fusion.ports.semantic_compiler import SemanticCompiler

logger = logging.getLogger(__name__)


class SemanticService:
    """Resolves metric questions against per-table semantic models."""

    def __init__(
        self,
        catalog: SchemaCatalog,
        query: QueryService,
        compiler: SemanticCompiler,
        models: Mapping[str, SemanticModel] | None = None,
        cross_check: bool = True,
    ) -> None:
        self._catalog = catalog
        self._query = query
        self._compiler = compiler
        #: Explicitly configured models, by full table name. Anything not in
        #: here is inferred from the catalog on first use.
        self._configured: dict[str, SemanticModel] = dict(models or {})
        self._inferred: dict[str, SemanticModel] = {}
        self._cross_check = cross_check

    # -- models -------------------------------------------------------------

    def model_for(self, table: str | TableRef) -> SemanticModel:
        """The model for a table: configured if there is one, else inferred.

        Raises:
            QueryError: When the table is not in the catalog, naming what is.
        """
        ref = self._resolve(table)
        name = ref.full_name
        configured = self._configured.get(name)
        if configured is not None:
            return configured
        cached = self._inferred.get(name)
        if cached is not None:
            return cached
        model = infer_model(ref, self._catalog.get_table(ref))
        self._inferred[name] = model
        return model

    def define(
        self, table: str | TableRef, measures: Sequence[Any], dimensions: Sequence[Any]
    ) -> SemanticModel:
        """Replace the model for a table (SDK only; not exposed as a tool).

        Deciding what "revenue" means is an operator's call, not a model's: a
        caller that defines its own measures and then queries them is checking
        its own homework, and nothing about the answer would be auditable.
        """
        ref = self._resolve(table)
        model = SemanticModel(
            ref=ref, measures=tuple(measures), dimensions=tuple(dimensions), source="explicit"
        )
        self._configured[ref.full_name] = model
        self._inferred.pop(ref.full_name, None)
        return model

    def describe(self, table: str | TableRef | None = None) -> dict[str, Any]:
        """The model for one table, or a summary of every table that has one."""
        if table is None:
            return {
                "tables": [
                    {
                        "table": ref.full_name,
                        "measures": list(self.model_for(ref).measure_names),
                        "dimensions": list(self.model_for(ref).dimension_names),
                    }
                    for ref in self._catalog.list_tables()
                ],
                "transforms": [spec.as_dict() for spec in TRANSFORMS.values()],
            }
        model = self.model_for(table)
        described = model.as_dict()
        described["transforms"] = [spec.as_dict() for spec in TRANSFORMS.values()]
        described["examples"] = self._examples(model)
        return described

    @staticmethod
    def _examples(model: SemanticModel) -> list[str]:
        """Metric expressions that will work against this model, ready to paste.

        Keys are skipped when there is anything else numeric: ``id:sum`` is a
        valid expression and a useless suggestion, and an example that reads as
        nonsense teaches the wrong thing about the whole language.
        """
        examples = ["*:count"]
        candidates = [m for m in model.measures if m.numeric and not m.is_row_count]
        numeric = next(
            (m for m in candidates if not _looks_like_a_key(m.name)),
            next(iter(candidates), None),
        )
        if numeric is not None:
            examples += [
                f"{numeric.name}:sum",
                f"{numeric.name}:avg",
                f"cumsum({numeric.name}:sum)",
                f"change_pct({numeric.name}:sum)",
            ]
            if any(d.temporal for d in model.dimensions):
                examples.append(f"time_shift({numeric.name}:sum)")
        return examples

    # -- running ------------------------------------------------------------

    def build(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> SemanticQuery:
        """Parse and validate a metric question against the table's model."""
        # Arguments arrive from a language model, so their *types* are as
        # untrusted as their values: a list where a string belongs used to
        # surface as an AttributeError from deep inside the domain.
        metrics = _string_list(metrics, "metrics", "metrics=['revenue:sum']")
        dimensions = _string_list(dimensions, "dimensions", "dimensions=['status']")
        if not isinstance(order_by, str):
            raise QueryError(
                f"order_by must be a single column name, got {order_by!r}. "
                f"Prefix it with '-' to sort descending, e.g. order_by='-revenue_sum'."
            )
        if not isinstance(filters, Sequence) or isinstance(filters, str):
            raise QueryError(
                f"filters must be a list of conditions, got {filters!r}. "
                f"For example: filters=[{{'column': 'status', 'value': 'paid'}}]."
            )
        model = self.model_for(table)
        query = SemanticQuery(
            model=model,
            metrics=tuple(parse_metric(metric) for metric in metrics),
            dimensions=tuple(parse_dimension(dim, model) for dim in dimensions),
            filters=tuple(_predicate(f) for f in filters),
            order_by=order_by,
            limit=min(int(limit or MAX_RESULT_ROWS), MAX_RESULT_ROWS),
        )
        query.validate(self._catalog.get_table(model.ref))
        return query

    def run(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> QueryResult:
        """Answer a metric question.

        Raises:
            QueryError: For a malformed metric, an unknown measure or column,
                or a table too large to answer for — the planner's refusal
                names the size, the limit and the ways forward.
        """
        query = self.build(table, metrics, dimensions, filters, order_by, limit)
        compiled, _ = self._materialize_and_compile(query)
        logger.info("Semantic query on %s: %s", query.model.ref.full_name, compiled.sql[:200])
        return self._query.run_local(compiled.sql)

    def explain(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> dict[str, Any]:
        """What ``run`` would do, without running it."""
        query = self.build(table, metrics, dimensions, filters, order_by, limit)
        compiled, tables = self._materialize_and_compile(query)
        return {
            "query": query.as_dict(),
            "sql": compiled.sql,
            "base_sql": compiled.base_sql,
            "columns": list(compiled.columns),
            "shape": query.query_shape().as_dict(),
            "reading": {ref.full_name: name for ref, name in tables.items()},
        }

    def _materialize_and_compile(self, query: SemanticQuery) -> tuple[Any, dict[TableRef, str]]:
        """Load the slices the query needs, then generate SQL against them."""
        shape = query.query_shape()
        tables = self._query.materialize_for(shape)
        if query.model.ref not in tables:
            raise QueryError(
                f"No connected source holds {query.model.ref.full_name}. "
                f"Call list_sources to see what is available."
            )
        compiled = self._compiler.compile(query, tables)
        if self._cross_check:
            self._verify_shape(query, compiled, shape)
        return compiled, tables

    def _verify_shape(self, query: SemanticQuery, compiled: Any, shape: Any) -> None:
        """Check the shape we planned from against the SQL we actually generated.

        Both come from the same query, so they agree by construction — but a
        disagreement would silently drop rows, which is the one failure this
        layer must not have. Reading the generated scan back with the analyzer
        costs one parse and turns that into a log line.
        """
        try:
            observed = self._query.analyzer.analyze(compiled.base_sql)
        except Exception as e:  # pragma: no cover - the analyzer swallows its own errors
            logger.warning("Could not cross-check the generated scan: %s", e)
            return
        merged = shape.merge_conservative(observed)
        if merged != shape:
            logger.warning(
                "Generated scan disagrees with the planned shape for %s; planned %s, read back %s",
                query.model.ref.full_name,
                shape.as_dict(),
                observed.as_dict(),
            )

    # -- helpers ------------------------------------------------------------

    def _resolve(self, table: str | TableRef) -> TableRef:
        """Resolve a table name against the catalog.

        An unqualified name takes the first source that has a table by that
        name, which is the rule the SQL planner already uses — so
        ``query_metrics('orders', ...)`` and ``FROM orders`` agree.
        """
        known = self._catalog.list_tables()
        if isinstance(table, str):
            validate_identifier(table, "table name")
            if "." not in table:
                match = next((ref for ref in known if ref.table == table), None)
                if match is not None:
                    return match
                raise QueryError(self._unknown_table(table, known))
        try:
            ref = coerce_ref(table)
        except SchemaError as e:
            raise QueryError(str(e)) from e
        if self._catalog.has_table(ref):
            return ref
        raise QueryError(self._unknown_table(str(table), known))

    @staticmethod
    def _unknown_table(table: str, known: Sequence[TableRef]) -> str:
        available = ", ".join(ref.full_name for ref in known) or "none"
        return f"Unknown table '{table}'. Connected tables: {available}."


def _predicate(raw: Mapping[str, Any]) -> Predicate:
    """Build a Predicate from a tool-supplied filter mapping.

    Raises:
        QueryError: On a missing column, an unknown operator, or a value the
            operator cannot use.
    """
    if not isinstance(raw, Mapping):
        raise QueryError(f"Each filter must be an object with 'column' and 'value', got {raw!r}.")
    column = raw.get("column")
    if not isinstance(column, str) or not column:
        raise QueryError(f"A filter needs a 'column', got {raw!r}.")
    validate_identifier(column, "column name")
    op = str(raw.get("op", "eq"))
    if op not in PREDICATE_OPS:
        raise QueryError(
            f"Unknown filter operator '{op}' on '{column}'. "
            f"Available: {', '.join(sorted(PREDICATE_OPS))}."
        )
    operator = cast(PredicateOp, op)
    if op != "is_null" and "value" not in raw:
        raise QueryError(f"Filter on '{column}' with op '{op}' needs a 'value'.")
    try:
        return Predicate(column=column, op=operator, value=raw.get("value"))
    except ValueError as e:
        raise QueryError(f"Invalid filter on '{column}': {e}") from e


def _looks_like_a_key(name: str) -> bool:
    """Whether a column name reads as an identifier rather than a quantity."""
    lowered = name.lower()
    return lowered == "id" or lowered.endswith(("_id", "_key", "_code"))


def _string_list(value: Any, label: str, example: str) -> tuple[str, ...]:
    """Coerce a tool argument that must be a list of names.

    ``None`` means "none given". A bare string is the mistake a model makes
    most often, and naming it is more useful than a type error from three
    frames deeper.

    Raises:
        QueryError: When the value is not a list of strings.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise QueryError(f"{label} must be a list, e.g. {example}.")
    if not isinstance(value, Sequence):
        raise QueryError(f"{label} must be a list, got {value!r}. For example: {example}.")
    for item in value:
        if not isinstance(item, str):
            raise QueryError(
                f"Every entry in {label} must be a name, got {item!r}. For example: {example}."
            )
    return tuple(value)
