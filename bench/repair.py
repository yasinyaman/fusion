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


def neighbours(request: Mapping[str, Any], branch: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Every request one field away from this one, within the schema.

    Reads the alternatives off the same schema branch the arm decoded against,
    so an edit can never produce something the layer would refuse.
    """
    metrics = list(request.get("metrics") or [])
    dimensions = list(request.get("dimensions") or [])
    filters = list(request.get("filters") or [])
    properties = branch.get("properties", {})
    metric_options = properties.get("metrics", {}).get("items", {}).get("oneOf", [])
    dimension_options = properties.get("dimensions", {}).get("items", {}).get("oneOf", [])
    filter_options = properties.get("filters", {}).get("items", {}).get("oneOf", [])

    # -- the metric: a different measure, aggregation or transform
    if metrics:
        first, rest = metrics[0], metrics[1:]
        transforms = [""]
        for option in metric_options:
            transforms = option["properties"].get("transform", {}).get("enum", [""])
            break
        for option in metric_options:
            measure = option["properties"]["measure"]["const"]
            for agg in option["properties"]["aggregation"]["enum"]:
                if (measure, agg) == (first.get("measure"), first.get("aggregation")):
                    continue
                edited = dict(first, measure=measure, aggregation=agg)
                yield _swap(request, metrics=[edited, *rest])
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

    # -- the breakdown: one added, one removed, one regrained
    for option in dimension_options:
        column = option["properties"]["column"]["const"]
        grains = option["properties"].get("grain", {}).get("enum", [""])
        for grain in grains:
            candidate = {"column": column} if not grain else {"column": column, "grain": grain}
            if candidate in dimensions:
                continue
            yield _swap(request, dimensions=[candidate])
            if dimensions:
                yield _swap(request, dimensions=[*dimensions, candidate])
    strands_a_transform = metrics and metrics[0].get("transform") in _calendar_transforms()
    if dimensions and not strands_a_transform:
        yield _swap(request, dimensions=[])
        for index in range(len(dimensions)):
            remaining = dimensions[:index] + dimensions[index + 1 :]
            yield _swap(request, dimensions=remaining)

    # -- the condition: one added, one removed
    for option in filter_options:
        properties_of = option["properties"]
        column = properties_of["column"]["const"]
        values = properties_of["value"].get("enum")
        if not values:
            continue
        for value in values:
            candidate = {"column": column, "op": "eq", "value": value}
            if candidate not in filters:
                yield _swap(request, filters=[*filters, candidate])
    if filters:
        yield _swap(request, filters=[])

    # -- the ordering
    if request.get("order_by"):
        yield _swap(request, descending=not request.get("descending"))


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
