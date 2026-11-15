"""The metric expression language: ``change_pct(cumsum(revenue:sum))``.

Grammar::

    expr        := transform | measure_ref
    transform   := ident "(" expr ( "," arg )* ")"
    measure_ref := ( ident | "*" ) [ ":" ident [ "(" arg ("," arg)* ")" ] ]
    arg         := ident "=" value
    value       := ident | number | "-" number | "'" text "'"
    ident       := [A-Za-z_][A-Za-z0-9_]*

Parsed by hand, because the domain layer may not import a parser library — and
because the alphabet is worth controlling directly. The only characters an
expression may contain are alphanumerics, ``_ : ( ) , = * ' - .`` and
whitespace, so it cannot express a quote escape or a statement separator; an
expression that reaches the compiler has already been proved to be a tree of
known transforms over a known measure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fusion.domain.errors import QueryError
from fusion.domain.measures import ROW_MEASURE, check_agg_args, normalize_agg
from fusion.domain.transforms import TransformSpec, transform_spec

#: How deeply transforms may nest. Bounds both the Python recursion and the
#: number of CTEs the compiler emits.
MAX_NESTING = 5

_IDENT_START = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
_IDENT_BODY = _IDENT_START + "0123456789"
_DIGITS = "0123456789"


@dataclass(frozen=True, slots=True)
class MeasureRef:
    """A measure and the aggregation applied to it: ``revenue:sum``."""

    measure: str
    agg: str
    args: tuple[tuple[str, Any], ...] = ()

    def output_name(self) -> str:
        """The column name this produces, always a valid bare identifier."""
        if self.measure == ROW_MEASURE:
            return self.agg.lower()
        return f"{self.measure}_{self.agg.lower()}"

    def as_text(self) -> str:
        base = f"{self.measure}:{self.agg.lower()}"
        if not self.args:
            return base
        inner = ", ".join(f"{name}={value}" for name, value in self.args)
        return f"{base}({inner})"


@dataclass(frozen=True, slots=True)
class TransformCall:
    """A transform wrapped around an inner expression: ``cumsum(revenue:sum)``."""

    name: str
    inner: MetricExpr
    args: tuple[tuple[str, Any], ...] = ()

    @property
    def spec(self) -> TransformSpec:
        return transform_spec(self.name)

    def output_name(self) -> str:
        return f"{self.name}_{self.inner.output_name()}"

    def as_text(self) -> str:
        parts = [self.inner.as_text()]
        parts.extend(f"{name}={value}" for name, value in self.args)
        return f"{self.name}({', '.join(parts)})"


MetricExpr = MeasureRef | TransformCall


def measure_ref_of(expr: MetricExpr) -> MeasureRef:
    """The measure at the bottom of an expression tree."""
    while isinstance(expr, TransformCall):
        expr = expr.inner
    return expr


def transforms_of(expr: MetricExpr) -> tuple[TransformCall, ...]:
    """The transforms wrapping a measure, innermost first."""
    chain: list[TransformCall] = []
    while isinstance(expr, TransformCall):
        chain.append(expr)
        expr = expr.inner
    return tuple(reversed(chain))


def depth(expr: MetricExpr) -> int:
    """How many transforms wrap the measure."""
    return len(transforms_of(expr))


def needs_time(expr: MetricExpr) -> bool:
    """Whether any transform in the tree needs a calendar grain."""
    return any(call.spec.needs == "calendar" for call in transforms_of(expr))


def needs_order(expr: MetricExpr) -> bool:
    """Whether any transform in the tree needs the rows to be ordered."""
    return bool(transforms_of(expr))


# --- tokenizer ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # ident | number | string | punct | end
    text: str
    position: int


def _caret(text: str, position: int) -> str:
    """The expression with a caret under ``position``, for an error message."""
    return f"\n  {text}\n  {' ' * position}^"


def _fail(text: str, position: int, message: str) -> QueryError:
    return QueryError(
        f"Cannot parse metric '{text}': {message} at position {position}.{_caret(text, position)}"
    )


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in _IDENT_START:
            start = index
            while index < len(text) and text[index] in _IDENT_BODY:
                index += 1
            tokens.append(_Token("ident", text[start:index], start))
            continue
        if char in _DIGITS or (
            char == "-" and index + 1 < len(text) and text[index + 1] in _DIGITS
        ):
            start = index
            index += 1
            while index < len(text) and (text[index] in _DIGITS or text[index] == "."):
                index += 1
            tokens.append(_Token("number", text[start:index], start))
            continue
        if char == "'":
            start = index
            index += 1
            while index < len(text) and text[index] != "'":
                index += 1
            if index >= len(text):
                raise _fail(text, start, "unterminated quoted value")
            index += 1
            tokens.append(_Token("string", text[start + 1 : index - 1], start))
            continue
        if char in "():,=*":
            tokens.append(_Token("punct", char, index))
            index += 1
            continue
        raise _fail(text, index, f"unexpected character {char!r}")
    tokens.append(_Token("end", "", len(text)))
    return tokens


# --- parser ---------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokenize(text)
        self.index = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.current
        if token.kind != "end":
            self.index += 1
        return token

    def at_punct(self, char: str) -> bool:
        return self.current.kind == "punct" and self.current.text == char

    def expect_punct(self, char: str) -> _Token:
        if not self.at_punct(char):
            found = self.current.text or "end of expression"
            raise _fail(self.text, self.current.position, f"expected '{char}', found {found!r}")
        return self.advance()

    def parse(self) -> MetricExpr:
        expr = self.parse_expr(level=0)
        if self.current.kind != "end":
            raise _fail(
                self.text,
                self.current.position,
                f"unexpected {self.current.text!r} after the metric",
            )
        return expr

    def parse_expr(self, level: int) -> MetricExpr:
        if level > MAX_NESTING:
            raise QueryError(
                f"Metric nesting is limited to {MAX_NESTING} levels; '{self.text}' goes deeper."
            )
        token = self.current
        # A transform is an identifier followed by '('; a measure never is.
        if token.kind == "ident" and self.tokens[self.index + 1].kind == "punct":
            if self.tokens[self.index + 1].text == "(":
                return self.parse_transform(level)
        return self.parse_measure_ref()

    def parse_transform(self, level: int) -> TransformCall:
        name_token = self.advance()
        spec = transform_spec(name_token.text, name_token.position)
        self.expect_punct("(")
        inner = self.parse_expr(level + 1)
        args = self.parse_args(closing=True)
        spec.bind(args)  # validate here, so the error names the position the user wrote
        return TransformCall(name=spec.name, inner=inner, args=args)

    def parse_measure_ref(self) -> MeasureRef:
        token = self.current
        if self.at_punct("*"):
            self.advance()
            measure = ROW_MEASURE
        elif token.kind == "ident":
            self.advance()
            measure = token.text
        else:
            found = token.text or "end of expression"
            raise _fail(self.text, token.position, f"expected a measure name, found {found!r}")
        if not self.at_punct(":"):
            raise _fail(
                self.text,
                self.current.position,
                f"expected ':' and an aggregation after '{measure}' (for example '{measure}:sum')",
            )
        self.advance()
        agg_token = self.current
        if agg_token.kind != "ident":
            found = agg_token.text or "end of expression"
            raise _fail(
                self.text, agg_token.position, f"expected an aggregation after ':', found {found!r}"
            )
        self.advance()
        agg = normalize_agg(agg_token.text, measure)
        args: tuple[tuple[str, Any], ...] = ()
        if self.at_punct("("):
            self.advance()
            args = self.parse_args(closing=True, leading_comma=False)
        # Checked here rather than at model-validation time: neither of these
        # depends on the model, and saying so now points at what was written.
        if measure == ROW_MEASURE and agg not in ("COUNT", "COUNT_DISTINCT"):
            raise QueryError(
                f"'{ROW_MEASURE}' counts rows, so it only takes count "
                f"(got '{agg.lower()}'). Use '{ROW_MEASURE}:count'."
            )
        check_agg_args(agg, args, measure)
        return MeasureRef(measure=measure, agg=agg, args=args)

    def parse_args(self, closing: bool, leading_comma: bool = True) -> tuple[tuple[str, Any], ...]:
        """Parse ``name=value`` pairs up to the closing parenthesis."""
        args: list[tuple[str, Any]] = []
        first = True
        while not self.at_punct(")"):
            if not (first and not leading_comma):
                self.expect_punct(",")
            first = False
            name_token = self.current
            if name_token.kind != "ident":
                found = name_token.text or "end of expression"
                raise _fail(
                    self.text, name_token.position, f"expected an argument name, found {found!r}"
                )
            self.advance()
            self.expect_punct("=")
            args.append((name_token.text, self.parse_value()))
        if closing:
            self.expect_punct(")")
        return tuple(args)

    def parse_value(self) -> Any:
        token = self.current
        if token.kind == "number":
            self.advance()
            return float(token.text) if "." in token.text else int(token.text)
        if token.kind in ("ident", "string"):
            self.advance()
            return token.text
        found = token.text or "end of expression"
        raise _fail(self.text, token.position, f"expected a value, found {found!r}")


def parse_metric(text: str) -> MetricExpr:
    """Parse one metric expression.

    Args:
        text: For example ``revenue:sum`` or ``change_pct(cumsum(revenue:sum))``.

    Returns:
        The expression tree.

    Raises:
        QueryError: With the offending position, a caret and — for an unknown
            transform or aggregation — the nearest match and the full list.
    """
    if not isinstance(text, str) or not text.strip():
        raise QueryError(
            "A metric cannot be empty. Write 'measure:aggregation', for example "
            "'revenue:sum' or '*:count'."
        )
    return _Parser(text.strip()).parse()
