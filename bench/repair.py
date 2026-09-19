"""How far a wrong answer is from a right one.

Execution accuracy asks whether an arm answered correctly on its own. That is
the metric for a system nobody watches. It is the wrong metric for a copilot,
where the question is not "was it right?" but "if it was wrong, how much work
is the person now facing?"

Two wrong answers can be very different. A request that asked for the wrong
aggregation is one field away from the answer, and a person fixes it by
changing one word. A query naming a column that does not exist is not near
anything: there is nothing to correct, only something to rewrite.

So this measures **one-edit repair**: of the requests an arm got wrong, how
many have a single-field change that produces the gold result. Every edit stays
inside the semantic model, so a repaired request is one the arm could have
produced and a person could have chosen.

This can only be computed for an arm that answers with a structured request.
The neighbours of ``{"measure": "tutar", "aggregation": "avg"}`` are
enumerable; the neighbours of a SELECT statement are not. That asymmetry is
not a limitation of the measurement — it is the property being measured.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from itertools import zip_longest
from typing import Any

#: A guard, not a tuning knob. The neighbourhood of a request is small by
#: construction; a number this large means something built it wrongly.
MAX_NEIGHBOURS = 400


def _calendar_transforms() -> frozenset[str]:
    """Transforms that need a time dimension, as the domain declares them.

    A copilot may not offer an edit that fails, so ``time_shift`` is withheld
    from a request with nothing to shift along. Which transforms those are is
    read from Fusion rather than listed here, so a new one is covered the day
    it lands.
    """
    from fusion.domain.transforms import TRANSFORMS

    return frozenset(
        name for name, spec in TRANSFORMS.items() if spec.as_dict().get("needs") == "calendar"
    )


def _has_a_grain(dimensions: Sequence[Mapping[str, Any]]) -> bool:
    return any(dimension.get("grain") for dimension in dimensions)


def _swap(request: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    edited = copy.deepcopy(dict(request))
    edited.update(changes)
    return edited


def _metric_edits(
    request: Mapping[str, Any],
    metrics: Sequence[Any],
    options: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """A different measure, aggregation or transform on the first metric.

    Skipped entirely when the metrics are plain DSL strings rather than the
    structured form: the unconstrained arm answers that way, `to_dsl` accepts
    it, and reaching for `.get` on a `str` used to raise out of the generator
    and past the caller's guard. The other edit kinds still apply, so a
    string-shaped request is measurable rather than a crash.
    """
    if not metrics or not isinstance(metrics[0], Mapping):
        return
    first, rest = metrics[0], list(metrics[1:])
    transforms: Sequence[str] = [""]
    for option in options:
        transforms = option["properties"].get("transform", {}).get("enum", [""])
        break
    for option in options:
        measure = option["properties"]["measure"]["const"]
        for agg in option["properties"]["aggregation"]["enum"]:
            if (measure, agg) == (first.get("measure"), first.get("aggregation")):
                continue
            yield _swap(request, metrics=[dict(first, measure=measure, aggregation=agg), *rest])
    calendar = _calendar_transforms()
    for transform in transforms:
        if transform in calendar and not _has_a_grain(dimensions):
            continue
        if transform != (first.get("transform") or ""):
            edited = dict(first)
            if transform:
                edited["transform"] = transform
            else:
                edited.pop("transform", None)
            yield _swap(request, metrics=[edited, *rest])
    if rest:
        yield _swap(request, metrics=[first])


def _dimension_edits(
    request: Mapping[str, Any],
    metrics: Sequence[Any],
    dimensions: Sequence[Mapping[str, Any]],
    options: Sequence[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """One breakdown added, removed or regrained — and only one.

    Replacing the whole list with a single candidate is only one edit when
    there was at most one to begin with. Offering it for a two-dimension
    request counted "drop both, add one" as a single-field fix, which
    overstated how near a wrong answer was to a right one.
    """
    for option in options:
        column = option["properties"]["column"]["const"]
        grains = option["properties"].get("grain", {}).get("enum", [""])
        for grain in grains:
            candidate = {"column": column} if not grain else {"column": column, "grain": grain}
            if candidate in dimensions:
                continue
            if len(dimensions) <= 1:
                yield _swap(request, dimensions=[candidate])
            if dimensions:
                yield _swap(request, dimensions=[*dimensions, candidate])
    first = metrics[0] if metrics and isinstance(metrics[0], Mapping) else {}
    if dimensions and first.get("transform") not in _calendar_transforms():
        # One at a time. Clearing the list outright is the same as this when
        # there is a single breakdown, and more than one edit when there are
        # two — which is the whole point of the loop.
        for index in range(len(dimensions)):
            yield _swap(request, dimensions=dimensions[:index] + list(dimensions[index + 1 :]))


def _filter_edits(
    request: Mapping[str, Any],
    filters: Sequence[Mapping[str, Any]],
    options: Sequence[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """One condition added or removed."""
    for option in options:
        properties = option["properties"]
        column = properties["column"]["const"]
        values = properties["value"].get("enum")
        if not values:
            continue
        for value in values:
            candidate = {"column": column, "op": "eq", "value": value}
            if candidate not in filters:
                yield _swap(request, filters=[*filters, candidate])
    if filters:
        yield _swap(request, filters=[])
    if request.get("order_by"):
        yield _swap(request, descending=not request.get("descending"))


def neighbours(request: Mapping[str, Any], branch: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Every request one field away from this one, within the schema.

    Reads the alternatives off the same schema branch the arm decoded against,
    so an edit can never produce something the layer would refuse.

    The three kinds are interleaved rather than concatenated, because the
    search that consumes this stops at a cap. Emitting every measure and
    aggregation swap first meant that on a wide table the cap was reached
    before a single filter edit was offered — and a missing condition is the
    commonest repair there is.
    """
    metrics = list(request.get("metrics") or [])
    dimensions = [d for d in (request.get("dimensions") or []) if isinstance(d, Mapping)]
    filters = list(request.get("filters") or [])
    properties = branch.get("properties", {})

    def options(field: str) -> Sequence[Mapping[str, Any]]:
        return properties.get(field, {}).get("items", {}).get("oneOf", [])

    streams = (
        _metric_edits(request, metrics, options("metrics"), dimensions),
        _dimension_edits(request, metrics, dimensions, options("dimensions")),
        _filter_edits(request, filters, options("filters")),
    )
    for group in zip_longest(*streams):
        for candidate in group:
            if candidate is not None:
                yield candidate


def branch_for(schema: Mapping[str, Any], table: str) -> dict[str, Any]:
    """The schema branch describing one table."""
    branches = schema.get("oneOf") or [schema]
    for branch in branches:
        if branch.get("properties", {}).get("table", {}).get("const") == table:
            return dict(branch)
    return dict(branches[0])


def one_edit_away(
    request: Mapping[str, Any],
    table: str,
    schema: Mapping[str, Any],
    run: Any,
    matches: Any,
) -> dict[str, Any] | None:
    """The single edit that would have made this request right, if there is one.

    Args:
        run: Executes a request and returns rows, or raises.
        matches: Grades rows against the gold result.

    Returns:
        The repaired request, or ``None`` when no single edit reaches the
        answer. The request itself is returned as-is if it was already right.
    """
    branch = branch_for(schema, table)
    for index, candidate in enumerate(neighbours(request, branch)):
        if index >= MAX_NEIGHBOURS:
            break
        try:
            rows = run(candidate)
        except Exception:
            continue
        if rows is not None and matches(rows):
            return candidate
    return None


def edit_distance_label(correct: bool, repaired: Mapping[str, Any] | None) -> str:
    """What a person would have to do, in three buckets."""
    if correct:
        return "correct"
    if repaired is not None:
        return "one edit away"
    return "needs rethinking"


def summarise(labels: Sequence[str]) -> dict[str, int]:
    """Counts per bucket, in the order a reader wants them."""
    order = ("correct", "one edit away", "needs rethinking")
    counted = {name: 0 for name in order}
    for label in labels:
        counted[label] = counted.get(label, 0) + 1
    return counted
