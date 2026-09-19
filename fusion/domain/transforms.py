"""The window transforms a metric expression may wrap around a measure.

Each :class:`TransformSpec` is metadata only — it says what a transform is
called, what arguments it takes and what it means. Turning one into SQL is the
compiler adapter's job, because only the adapter may import sqlglot.

The registry is deliberately small and closed. A transform that is not listed
here cannot be parsed, so an LLM cannot invent one and have it reach SQL.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Literal

from fusion.domain.errors import QueryError

#: What a transform needs in order to have a meaning.
#: ``order`` — any ordering will do (ranking).
#: ``sequence`` — the previous *result row* (lag/lead/cumulative).
#: ``calendar`` — the previous *time period*, so a time dimension with a grain
#: must be present.
TransformNeeds = Literal["order", "sequence", "calendar"]


@dataclass(frozen=True, slots=True)
class TransformParam:
    """One keyword argument of a transform."""

    name: str
    kind: Literal["int"] = "int"
    default: int | None = None
    minimum: int = 1
    example: str = ""

    @property
    def required(self) -> bool:
        return self.default is None

    def coerce(self, value: Any, transform: str) -> int:
        """Validate one supplied argument value.

        Raises:
            QueryError: When the value is not a whole number at or above
                ``minimum``. ``bool`` is rejected explicitly because it is an
                ``int`` subclass and ``lag(x, n=True)`` is never intended.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise QueryError(
                f"{transform}'s '{self.name}' must be a whole number, got {value!r}. "
                f"For example: {self.example}"
            )
        if value < self.minimum:
            raise QueryError(
                f"{transform}'s '{self.name}' must be at least {self.minimum}, got {value}. "
                f"For example: {self.example}"
            )
        return value


@dataclass(frozen=True, slots=True)
class TransformSpec:
    """One window transform: its name, arguments and what it means."""

    name: str
    needs: TransformNeeds
    description: str
    params: tuple[TransformParam, ...] = field(default_factory=tuple)

    def param(self, name: str) -> TransformParam | None:
        for param in self.params:
            if param.name == name:
                return param
        return None

    def bind(self, args: tuple[tuple[str, Any], ...]) -> dict[str, int]:
        """Validate supplied arguments and fill in the defaults.

        Raises:
            QueryError: On an unknown argument name, a bad value, or a missing
                required argument.
        """
        supplied = dict(args)
        for name in supplied:
            if self.param(name) is None:
                allowed = ", ".join(p.name for p in self.params) or "none"
                raise QueryError(
                    f"{self.name} has no argument '{name}'. Accepted arguments: {allowed}."
                )
        bound: dict[str, int] = {}
        for param in self.params:
            if param.name in supplied:
                bound[param.name] = param.coerce(supplied[param.name], self.name)
            elif param.default is not None:
                bound[param.name] = param.default
            else:
                raise QueryError(
                    f"{self.name} needs a '{param.name}' argument. For example: {param.example}"
                )
        return bound

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "needs": self.needs,
            "arguments": [
                {"name": p.name, "required": p.required, "default": p.default} for p in self.params
            ],
        }


_OFFSET = TransformParam(name="n", default=1, example="lag(revenue:sum, n=2)")
_PERIODS = TransformParam(name="periods", default=1, example="time_shift(revenue:sum, periods=1)")
_BUCKETS = TransformParam(name="buckets", example="ntile(revenue:sum, buckets=4)")

TRANSFORMS: dict[str, TransformSpec] = {
    spec.name: spec
    for spec in (
        TransformSpec(
            name="cumsum",
            needs="sequence",
            description="Running total over the rows in order.",
        ),
        TransformSpec(
            name="change",
            needs="sequence",
            params=(_OFFSET,),
            description="Difference from the value n result rows earlier.",
        ),
        TransformSpec(
            name="change_pct",
            needs="sequence",
            params=(_OFFSET,),
            description=(
                "Fractional change from the value n result rows earlier "
                "(NULL when that value is zero)."
            ),
        ),
        TransformSpec(
            name="lag",
            needs="sequence",
            params=(_OFFSET,),
            description="The value n result rows earlier.",
        ),
        TransformSpec(
            name="lead",
            needs="sequence",
            params=(_OFFSET,),
            description="The value n result rows later.",
        ),
        TransformSpec(
            name="time_shift",
            needs="calendar",
            params=(_PERIODS,),
            description=(
                "The value exactly n calendar periods earlier, by the time "
                "dimension's grain. NULL when that period has no row — unlike "
                "lag, which would return whatever the previous row happens to be."
            ),
        ),
        TransformSpec(
            name="rank",
            needs="order",
            description="Rank by the value, highest first; ties share a rank and leave gaps.",
        ),
        TransformSpec(
            name="dense_rank",
            needs="order",
            description="Rank by the value, highest first; ties share a rank with no gaps.",
        ),
        TransformSpec(
            name="percent_rank",
            needs="order",
            description="Relative rank by the value, from 0.0 to 1.0.",
        ),
        TransformSpec(
            name="ntile",
            needs="order",
            params=(_BUCKETS,),
            description="Which of n equal-sized buckets the value falls into, highest first.",
        ),
    )
}

TRANSFORM_NAMES: tuple[str, ...] = tuple(sorted(TRANSFORMS))


def transform_spec(name: str, position: int = 0) -> TransformSpec:
    """Look up a transform by name.

    Raises:
        QueryError: Naming the position, the nearest match and every
            alternative, so the caller can fix the expression without a
            second round trip.
    """
    spec = TRANSFORMS.get(name)
    if spec is not None:
        return spec
    message = f"Unknown transform '{name}' at position {position}."
    close = difflib.get_close_matches(name, TRANSFORM_NAMES, n=1, cutoff=0.6)
    if close:
        message += f" Did you mean '{close[0]}'?"
    raise QueryError(f"{message} Available transforms: {', '.join(TRANSFORM_NAMES)}.")
