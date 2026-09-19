"""Turning a semantic model into a decoding constraint.

The first run failed the semantic arm on 17 of 40 questions, and every one of
those failures was a *form* error: a metric written ``sum(tutar)`` instead of
``tutar:sum``, a dimension the table does not have, an ``order_by`` handed
over as a list of objects. Not one was a wrong choice of measure.

Form errors are the kind a schema can make unrepresentable, and a semantic
model already holds exactly what such a schema needs. So instead of teaching a
model the DSL — by fine-tuning it, or by retrieving examples into the prompt —
the *shape* of the request is constrained while the model decodes, and the DSL
string is assembled here from choices it could not get syntactically wrong.

This is the one advantage arm C has that arms A and B structurally cannot:
the legal metrics of a semantic model can be enumerated; the legal SQL
statements against a schema cannot.

What it does **not** do is make an answer correct. A constrained model picks a
real measure — not necessarily the right one. Constraining removes the form
errors so that what remains is the interesting question: can the model map a
business question onto the right measure? That residual is what a retrieval
step or a fine-tune would have to earn its keep against.

Every rule here is read from the domain rather than restated: which
aggregations a measure accepts comes from ``Measure.check_agg``, so a rule
change in Fusion cannot leave a stale copy behind in the benchmark.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Transforms offered to a constrained model.
#:
#: ``ntile`` is left out deliberately: it is the only transform with a
#: *required* argument, and a schema that let the model pick it without one
#: would reintroduce exactly the class of error this module exists to remove.
#: Every other transform has a working default.
EXCLUDED_TRANSFORMS = frozenset({"ntile"})

#: Filter operators the arm offers. A subset of the DSL's, kept to the ones a
#: question can actually ask for without a second value.
FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "like")

#: Aggregations considered for each measure. Which of them survive is decided
#: per measure by the domain, not here.
CANDIDATE_AGGS = ("sum", "avg", "min", "max", "count", "count_distinct", "median")

#: A dimension with at most this many distinct values has its values offered as
#: an enum, so a filter cannot compare against one that was never stored.
#:
#: This is a fairness correction as much as an improvement. The catalog arm's
#: context spells out ``durum: basarili | iptal`` and ``kanal: mobil | web |
#: sube``; Fusion's semantic model does not carry values at all, so the first
#: run had the semantic arm guessing English values against Turkish data —
#: `durum = 'canceld'` where the column holds `iptal`. Withholding from one arm
#: what another is handed measures the harness, not the layer.
MAX_ENUMERABLE_VALUES = 25


def legal_aggregations(measure: Any) -> list[str]:
    """The aggregations this measure actually accepts.

    Asks the domain rather than re-deriving the rule: ``check_agg`` is what
    ``SemanticQuery.validate`` will apply later, so anything that survives
    here is guaranteed to survive there.
    """
    allowed = []
    for agg in CANDIDATE_AGGS:
        try:
            measure.check_agg(agg)
        except Exception:
            continue
        allowed.append(agg)
    return allowed


def _metric_branch(measure: Any, transforms: Sequence[str]) -> dict[str, Any]:
    """One ``oneOf`` branch: this measure, with only the aggregations it takes.

    Splitting per measure is what makes the constraint exact. A single
    ``{measure: enum, aggregation: enum}`` object would allow ``segment:sum``,
    which the schema would accept and the domain would then reject.
    """
    properties: dict[str, Any] = {
        "measure": {"const": measure.name},
        "aggregation": {"enum": legal_aggregations(measure)},
    }
    if transforms:
        properties["transform"] = {"enum": ["", *transforms]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["measure", "aggregation"],
        "additionalProperties": False,
    }


def dimension_values(semantic: Any, table: str, dimension: Any) -> list[Any]:
    """The values of a dimension that is a category, or an empty list.

    Asks through the DSL itself — a grouped row count *is* the dimension's
    domain — so discovery needs no second query path and honours the same
    slice rules as any other read.

    Two things are excluded, and the grouped count decides both. A dimension
    with too many values is not worth enumerating. And a dimension whose every
    value occurs exactly once is an identifier rather than a category: a
    transaction id, an amount, an email address. Enumerating those would leak
    per-row data into a prompt — the email column in this fixture is the case
    that makes the rule worth stating — and would teach the model nothing,
    because a vocabulary that never repeats is not a vocabulary.
    """
    described = dimension.as_dict()
    if described.get("temporal"):
        return []
    try:
        result = semantic.run(
            table, ["*:count"], [described["name"]], limit=MAX_ENUMERABLE_VALUES + 1
        )
    except Exception:
        return []
    rows = result.rows if hasattr(result, "rows") else result.get("rows", [])
    if not rows or len(rows) > MAX_ENUMERABLE_VALUES:
        return []
    columns = list(result.columns if hasattr(result, "columns") else result.get("columns", []))
    name = described["name"]
    if name not in columns or len(columns) < 2:
        return []
    # Rows come back as tuples from the service and as mappings from the tool
    # layer; reading by column position works for both.
    index, counts = columns.index(name), columns[-1]
    pairs = [
        (row[name], row[counts]) if isinstance(row, Mapping) else (row[index], row[-1])
        for row in rows
    ]
    # NULL is not part of the vocabulary, and it must not prop up the
    # "does it repeat?" test either: an email column with two missing
    # addresses would otherwise pass on the strength of its NULL group.
    present = [(value, count) for value, count in pairs if value is not None]
    if not present or all(_as_count(count) <= 1 for _, count in present):
        return []
    return sorted((value for value, _ in present), key=str)


def _as_count(value: Any) -> int:
    """A row count as an int, however the store typed it."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dimension_branch(dimension: Any) -> dict[str, Any]:
    """One ``oneOf`` branch per dimension.

    A grain is only offered on a temporal dimension — the property is absent
    from the other branches, so ``kanal:month`` cannot be expressed at all.
    """
    described = dimension.as_dict()
    properties: dict[str, Any] = {"column": {"const": described["name"]}}
    grains = described.get("grains") or []
    if grains:
        properties["grain"] = {"enum": ["", *grains]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["column"],
        "additionalProperties": False,
    }


def orderable_columns(model: Any, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """The names a result of this table could be ordered by.

    Built by asking the domain what each choice would be called rather than
    re-deriving the convention here: a measure and an aggregation become one
    output column, and only the domain knows how it is spelled.

    Transform outputs are left out to keep the list short — ordering by a
    running total is rare, and the enum has to stay small enough to be a
    grammar. What this removes is the whole class of failure that ``order_by``
    produced when it was free text: a model writing ``tutar DESC``, or
    ``rank(tutar, 'asc')``, into a field that wanted a column name. Direction
    is a separate boolean, so it has no reason to be spelled into the name.
    """
    from fusion.domain.metric_dsl import parse_metric
    from fusion.domain.semantic_query import parse_dimension

    names = [""]
    for dimension in model.dimensions:
        described = dimension.as_dict()
        for grain in described.get("grains") or [""]:
            name = f"{described['name']}:{grain}" if grain else described["name"]
            try:
                names.append(parse_dimension(name, model).output_name())
            except Exception:
                continue
    for measure, agg in pairs:
        try:
            names.append(parse_metric(f"{measure}:{agg}").output_name())
        except Exception:
            continue
    return list(dict.fromkeys(names))


def _filter_branches(model: Any, values: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """One filter branch per column, with its stored values when they are known.

    A column whose values are enumerated can only be compared against one of
    them, which removes the last place a constrained request could still name
    something that does not exist.
    """
    branches = []
    for dimension in model.dimensions:
        name = dimension.as_dict()["name"]
        known = values.get(name)
        branches.append(
            {
                "type": "object",
                "properties": {
                    "column": {"const": name},
                    "op": {"enum": ["eq", "ne"] if known else list(FILTER_OPS)},
                    "value": (
                        {"enum": list(known)}
                        if known
                        else {"type": ["string", "number", "boolean"]}
                    ),
                },
                "required": ["column", "op", "value"],
                "additionalProperties": False,
            }
        )
    return branches


def table_branch(
    model: Any, transforms: Sequence[str], values: Mapping[str, Sequence[Any]] | None = None
) -> dict[str, Any]:
    """The request schema for one table."""
    values = values or {}
    pairs = [(m.name, agg) for m in model.measures for agg in legal_aggregations(m)]
    return {
        "type": "object",
        "properties": {
            "table": {"const": model.ref.full_name},
            "metrics": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"oneOf": [_metric_branch(m, transforms) for m in model.measures]},
            },
            "dimensions": {
                "type": "array",
                "maxItems": 3,
                "items": {"oneOf": [_dimension_branch(d) for d in model.dimensions]},
            },
            "filters": {
                "type": "array",
                "maxItems": 2,
                "items": {"oneOf": _filter_branches(model, values)},
            },
            # Enumerated, after free text was measured and found to be the
            # last hole in the constraint: it produced 10 of 40 failures, all
            # of them a model writing SQL — `tutar DESC` — where a column name
            # belonged. Ordering by a name this particular query did not
            # select is still expressible, and is a real mistake worth seeing.
            "order_by": {"enum": orderable_columns(model, pairs)},
            "descending": {"type": "boolean"},
        },
        "required": ["table", "metrics"],
        "additionalProperties": False,
    }


def request_schema(semantic: Any, tables: Sequence[str]) -> dict[str, Any]:
    """A JSON schema admitting exactly the requests these tables can answer.

    One ``oneOf`` branch per table, because measures and dimensions differ
    between them and a union would let a model ask for one table's column on
    another's.
    """
    transforms = [
        spec["name"]
        for spec in semantic.describe(tables[0])["transforms"]
        if spec["name"] not in EXCLUDED_TRANSFORMS
    ]
    branches = []
    for table in tables:
        model = semantic.model_for(table)
        values = {
            d.as_dict()["name"]: found
            for d in model.dimensions
            if (found := dimension_values(semantic, table, d))
        }
        branches.append(table_branch(model, transforms, values))
    if len(branches) == 1:
        return branches[0]
    return {"oneOf": branches}


def _metric_to_dsl(metric: Any) -> str:
    """``{'measure': 'tutar', 'aggregation': 'sum'}`` -> ``'tutar:sum'``."""
    if isinstance(metric, str):
        return metric
    if not isinstance(metric, Mapping):
        return str(metric)
    measure = str(metric.get("measure", "")).strip()
    agg = str(metric.get("aggregation", "")).strip()
    expression = f"{measure}:{agg}" if agg else measure
    transform = str(metric.get("transform", "")).strip()
    return f"{transform}({expression})" if transform else expression


def _dimension_to_dsl(dimension: Any) -> str:
    """``{'column': 'islem_tarihi', 'grain': 'month'}`` -> ``'islem_tarihi:month'``."""
    if isinstance(dimension, str):
        return dimension
    if not isinstance(dimension, Mapping):
        return str(dimension)
    column = str(dimension.get("column", "")).strip()
    grain = str(dimension.get("grain", "")).strip()
    return f"{column}:{grain}" if grain else column


def to_dsl(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a model reply into the arguments ``query_metrics`` takes.

    Accepts both shapes — the structured one a constrained model produces and
    the plain strings an unconstrained one writes — so the arm has a single
    execution path and the two configurations stay comparable.
    """
    metrics = [_metric_to_dsl(m) for m in request.get("metrics") or []]
    dimensions = [_dimension_to_dsl(d) for d in request.get("dimensions") or []]
    order_by = request.get("order_by") or ""
    if isinstance(order_by, str) and order_by and request.get("descending"):
        order_by = f"-{order_by.lstrip('-')}"
    return {
        "table": request.get("table"),
        "metrics": [m for m in metrics if m],
        "dimensions": [d for d in dimensions if d],
        "filters": list(request.get("filters") or []),
        "order_by": order_by,
        "limit": request.get("limit", 100),
    }
