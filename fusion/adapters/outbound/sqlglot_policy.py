"""sqlglot-backed SqlValidator (guardrails) and SqlAnalyzer (shape + rewrite)."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from typing import Any

import sqlglot
from sqlglot import exp

from fusion.domain.errors import GuardrailViolation
from fusion.domain.models import MV_PREFIX, TableRef
from fusion.domain.query_shape import UNKNOWN_SHAPE, JoinEquality, QueryShape, TableUse
from fusion.domain.slices import Predicate, PredicateOp
from fusion.domain.sql_text import (
    FORBIDDEN_FUNCTIONS,
    find_forbidden_function,
    has_multiple_statements,
)

logger = logging.getLogger(__name__)

#: Comparisons that can be pushed to a source, and how to flip them when the
#: literal is written on the left (``10 < amount`` means ``amount > 10``).
_COMPARISONS: dict[type[exp.Expr], PredicateOp] = {
    exp.EQ: "eq",
    exp.NEQ: "ne",
    exp.GT: "gt",
    exp.GTE: "gte",
    exp.LT: "lt",
    exp.LTE: "lte",
}
_FLIPPED: dict[PredicateOp, PredicateOp] = {
    "eq": "eq",
    "ne": "ne",
    "gt": "lt",
    "gte": "lte",
    "lt": "gt",
    "lte": "gte",
}

# Any query expression: SELECT, set operations (UNION/INTERSECT/EXCEPT) and
# parenthesised subqueries. DDL/DML/commands are not Query subclasses.
_ALLOWED_TYPES: tuple[type, ...] = (exp.Query,)

#: ``EXPLAIN``, ``EXPLAIN ANALYZE`` and ``EXPLAIN (FORMAT JSON, ...)``.
_EXPLAIN_RE = re.compile(r"EXPLAIN\s+(ANALYZE\s+)?(\([^()]*\)\s*)?", re.IGNORECASE)

#: The dialect user SQL is executed in, and therefore the one it is read in.
#: The generic dialect reads ``E'\''`` as an unterminated string and would hide
#: a second statement behind it from every check below.
_DUCKDB = "duckdb"


class SqlglotValidator:
    """Allows only read-only query statements.

    Layers, in order: empty check, EXPLAIN unwrapping, multi-statement
    detection, forbidden function denylist (file/network/extension access),
    then an AST allowlist and a check of the parsed tree for file reads. SQL
    sqlglot cannot parse is refused.
    """

    def __init__(self, allow_create_mv: bool = False) -> None:
        self._allow_create_mv = allow_create_mv

    def validate(self, sql: str) -> None:
        stripped = sql.strip()
        if not stripped:
            raise GuardrailViolation("Empty SQL query")

        # EXPLAIN ANALYZE executes what it explains, so the statement
        # underneath has to pass on its own. A loop, not recursion: a
        # thousand nested EXPLAINs must be a refusal, not a RecursionError.
        explained = _EXPLAIN_RE.match(stripped)
        if explained:
            while explained:
                stripped = stripped[explained.end() :].strip()
                explained = _EXPLAIN_RE.match(stripped)
            self.validate(stripped)
            return

        if has_multiple_statements(stripped):
            raise GuardrailViolation(
                f"Multi-statement SQL detected (possible injection): {stripped[:100]}"
            )

        forbidden = find_forbidden_function(stripped)
        if forbidden:
            raise GuardrailViolation(
                f"Blocked forbidden function '{forbidden}()'. "
                f"File, network, and extension access is not allowed: {stripped[:100]}"
            )

        try:
            # Read as DuckDB reads it, and strictly: a parse that skips what it
            # cannot read could skip the part that writes.
            parsed = sqlglot.parse(stripped, read=_DUCKDB)
        except Exception:
            # What cannot be read cannot be shown to be a query.
            raise GuardrailViolation(
                f"Blocked SQL that could not be parsed: {stripped[:100]}"
            ) from None

        statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
        if not statements:
            raise GuardrailViolation("Failed to parse SQL query")
        if len(statements) > 1:
            raise GuardrailViolation(
                f"Multi-statement SQL detected (possible injection): {stripped[:100]}"
            )
        self._validate_statement(statements[0], stripped)

    def _validate_statement(self, statement: exp.Expr, original_sql: str) -> None:
        if isinstance(statement, _ALLOWED_TYPES):
            if statement.args.get("into"):
                raise GuardrailViolation(
                    f"Blocked SELECT INTO statement. Only SELECT queries are allowed: "
                    f"{original_sql[:100]}"
                )
            if any(node.args.get("locks") for node in statement.find_all(exp.Select)):
                # FOR UPDATE / FOR SHARE take row locks at a source it is pushed to.
                raise GuardrailViolation(
                    f"Blocked SELECT ... FOR UPDATE/SHARE: only plain reads are allowed: "
                    f"{original_sql[:100]}"
                )
            _check_no_file_access(statement, original_sql)
            return

        if self._allow_create_mv and isinstance(statement, exp.Create):
            table_name = str(statement.this) if statement.this else ""
            if table_name.startswith(MV_PREFIX):
                return

        raise GuardrailViolation(
            f"Blocked {type(statement).__name__} statement. Only SELECT queries are allowed: "
            f"{original_sql[:100]}"
        )


#: A table name DuckDB would open as a file or URL instead of looking up.
_PATH_LIKE = re.compile(r"[/\\:*?.\[]")


def _check_no_file_access(statement: exp.Expr, original_sql: str) -> None:
    """The text-level denylist again, on the parsed tree.

    The text check can be talked past — a quoted function name, an apostrophe
    in a comment — and it never sees ``FROM '/etc/passwd'``, which is a file
    read with no function call in it at all.
    """
    for node in statement.walk():
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            if name.lower() in FORBIDDEN_FUNCTIONS:
                raise GuardrailViolation(
                    f"Blocked forbidden function '{name.lower()}()'. "
                    f"File, network, and extension access is not allowed: {original_sql[:100]}"
                )
        elif isinstance(node, exp.Table) and _PATH_LIKE.search(node.name):
            raise GuardrailViolation(
                f"Blocked table name '{node.name}': it reads as a file or URL, and "
                f"file and network access is not allowed: {original_sql[:100]}"
            )


class SqlglotAnalyzer:
    """Extracts table references, query shape, and rewrites table names.

    ``analyze`` is what makes slicing possible: it answers which columns and
    which literal conditions belong to each table. It refuses to guess — a
    query it cannot fully account for comes back as "not a simple select",
    and the planner then behaves exactly as it did before slices existed.
    """

    def table_references(self, sql: str) -> list[TableRef]:
        try:
            parsed = sqlglot.parse(sql, read=_DUCKDB, error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            logger.warning("sqlglot failed to parse SQL; no table references extracted")
            return []

        statements = [s for s in (parsed or []) if s is not None]
        cte_names = {
            cte.alias.lower()
            for statement in statements
            for cte in statement.find_all(exp.CTE)
            if cte.alias
        }

        refs: list[TableRef] = []
        for statement in statements:
            for table in statement.find_all(exp.Table):
                name = table.name
                if not name or name.lower() in cte_names:
                    continue
                ref = TableRef(table.db or "", name)
                if ref not in refs:
                    refs.append(ref)
        return refs

    def strip_source_prefix(self, sql: str, source: str) -> str:
        """Drop ``source.`` qualifiers by editing the text, not regenerating it.

        The SQL goes to the source, so everything else must arrive as written:
        a regeneration drops ``NULLS LAST`` (sqlglot's default, not Postgres's)
        and rewrites dialect-specific syntax.
        """
        from sqlglot.tokens import TokenType

        try:
            tokens = sqlglot.Dialect.get_or_raise(_DUCKDB).tokenize(sql)
        except Exception:
            return sql
        cuts: list[tuple[int, int]] = []
        for here, after in zip(tokens, tokens[1:], strict=False):
            name = here.text.strip('"').lower()
            if (
                here.token_type in (TokenType.VAR, TokenType.IDENTIFIER)
                and name == source.lower()
                and after.token_type == TokenType.DOT
            ):
                cuts.append((here.start, after.end + 1))
        for start, end in reversed(cuts):
            sql = sql[:start] + sql[end:]
        return sql

    # -- query shape --------------------------------------------------------

    def analyze(self, sql: str) -> QueryShape:
        """Columns, pushable predicates and join keys, per table."""
        try:
            # Strict, as DuckDB: a lenient parse that stops early reads a WHERE
            # cut short, and pushes a condition that sits under an OR.
            parsed = sqlglot.parse_one(sql, read=_DUCKDB)
        except Exception:
            logger.debug("sqlglot could not parse the query; treating it as opaque")
            return UNKNOWN_SHAPE
        if not isinstance(parsed, exp.Select) or not _is_simple(parsed):
            return UNKNOWN_SHAPE

        tables = _source_tables(parsed)
        if tables is None or not _joins_are_plain(parsed):
            return UNKNOWN_SHAPE
        if not tables:
            return QueryShape(limit=_limit_of(parsed))

        aliases = {alias: ref for alias, ref in tables}
        null_side = _null_side_aliases(parsed, [alias for alias, _ in tables])
        columns = _columns_by_alias(parsed, aliases)
        predicates, joins = _where_predicates(parsed, aliases, null_side)
        # A LIMIT may only be read at the source when the source applies every
        # condition: rows it would cut could be the only ones a local filter keeps.
        limit = _limit_of(parsed) if _where_fully_pushed(parsed, predicates) else None
        _join_conditions(parsed, aliases, null_side, predicates, joins)

        counts: dict[TableRef, int] = {}
        for _, ref in tables:
            counts[ref] = counts.get(ref, 0) + 1
        uses = []
        for alias, ref in tables:
            if counts[ref] > 1:
                # The same table under two aliases carries two different sets
                # of conditions; one slice cannot serve both, so take it whole.
                uses.append(TableUse(ref=ref, alias=alias, outer_null_side=alias in null_side))
                continue
            uses.append(
                TableUse(
                    ref=ref,
                    alias=alias,
                    columns=columns.get(alias),
                    predicates=tuple(predicates.get(alias, ())),
                    outer_null_side=alias in null_side,
                )
            )
        return QueryShape(
            tables=tuple(uses),
            joins=tuple(joins),
            limit=limit,
            is_simple_select=True,
            is_ordered=parsed.args.get("order") is not None,
            is_aggregated=_is_aggregated(parsed),
        )

    # -- rewriting ----------------------------------------------------------

    def with_row_cap(self, sql: str, rows: int) -> str:
        """Append ``LIMIT rows`` to a query that has none of its own.

        Appended as text rather than regenerated: the SQL goes to the source in
        the source's own dialect, which a round trip through sqlglot could change.
        """
        try:
            parsed = sqlglot.parse_one(sql, read=_DUCKDB)
        except Exception:
            return sql
        if not isinstance(parsed, exp.Select | exp.SetOperation):
            return sql
        if any(parsed.args.get(key) for key in ("limit", "offset", "fetch")):
            return sql
        # A newline, so a trailing ``-- comment`` cannot swallow the clause.
        return f"{sql.rstrip().rstrip(';').rstrip()}\nLIMIT {int(rows)}"

    def rewrite_tables(self, sql: str, mapping: Mapping[TableRef, str]) -> str:
        """Point table references at the tables named in ``mapping``."""
        if not mapping:
            return sql
        try:
            # Read and written as DuckDB: the rewritten SQL runs on the store,
            # and the generic dialect turns ``a // 2`` into ``a /  / 2`` and
            # ``* EXCLUDE (b)`` into ``* EXCEPT (b)``.
            parsed = sqlglot.parse_one(sql, read="duckdb", error_level=sqlglot.ErrorLevel.IGNORE)
        except Exception:
            return sql
        if parsed is None:
            return sql
        changed = False
        for table in parsed.find_all(exp.Table):
            ref = TableRef(table.db or "", table.name)
            target = mapping.get(ref)
            if target is None or target in (ref.full_name, table.name):
                continue
            if not table.args.get("alias"):
                # Keep the original name usable as a qualifier: the query may
                # say ``orders.id`` even though it now reads a slice table.
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            schema, _, name = target.rpartition(".")
            table.set("this", exp.to_identifier(name))
            table.set("db", exp.to_identifier(schema) if schema else None)
            changed = True
        return parsed.sql(dialect="duckdb") if changed else sql


# -- analysis helpers --------------------------------------------------------


def _is_simple(select: exp.Select) -> bool:
    """Only a single, flat SELECT is analyzed; anything else loads whole tables."""
    if select.args.get("with"):
        return False
    if any(node is not select for node in select.find_all(exp.Select)):
        return False
    return not any(True for _ in select.find_all(exp.Window))


def _source_tables(select: exp.Select) -> list[tuple[str, TableRef]] | None:
    """``(alias, ref)`` for the FROM table and every join, or None if unsupported."""
    tables: list[tuple[str, TableRef]] = []
    from_clause = select.find(exp.From)
    if from_clause is None:
        return tables
    if not isinstance(from_clause.this, exp.Table):
        return None
    for node in (from_clause.this, *(j.this for j in select.args.get("joins") or [])):
        if not isinstance(node, exp.Table):
            return None
        alias = node.args.get("alias")
        if alias is not None and alias.args.get("columns"):
            # ``t AS b(x, y)`` renames the columns: ``x`` is not the column x.
            return None
        tables.append((node.alias_or_name, TableRef(node.db or "", node.name)))
    return tables


def _joins_are_plain(select: exp.Select) -> bool:
    """False for joins whose rows are not "both sides matched on the ON clause".

    ANTI keeps the rows that do *not* match, so narrowing either side changes
    the answer; USING and NATURAL name their key columns outside any column
    reference, so the slice would be read without them.
    """
    for join in select.args.get("joins") or []:
        # Not an allowlist of kinds: sqlglot puts the optional OUTER keyword
        # in ``kind``, and LEFT OUTER JOIN is LEFT JOIN (``side`` covers it).
        if (join.kind or "").upper() in ("SEMI", "ANTI"):
            return False
        if join.method or join.args.get("using"):
            return False
    return True


def _where_fully_pushed(select: exp.Select, predicates: Mapping[str, list[Predicate]]) -> bool:
    where = select.args.get("where")
    conditions = len(list(_conjuncts(where.this))) if where is not None else 0
    return conditions == sum(len(found) for found in predicates.values())


def _null_side_aliases(select: exp.Select, order: list[str]) -> set[str]:
    """Aliases whose rows an outer join may NULL-extend (never filter those)."""
    null_side: set[str] = set()
    seen = order[:1]
    for index, join in enumerate(select.args.get("joins") or [], start=1):
        side = (join.side or "").upper()
        right = order[index] if index < len(order) else ""
        if side == "LEFT":
            null_side.add(right)
        elif side == "RIGHT":
            null_side.update(seen)
        elif side == "FULL":
            null_side.update(seen)
            null_side.add(right)
        if right:
            seen.append(right)
    null_side.discard("")
    return null_side


def _columns_by_alias(
    select: exp.Select, aliases: Mapping[str, TableRef]
) -> dict[str, frozenset[str] | None]:
    """Referenced columns per alias; ``None`` means "all of them"."""
    found: dict[str, set[str]] = {alias: set() for alias in aliases}
    unrestricted: set[str] = set()
    only_alias = next(iter(aliases)) if len(aliases) == 1 else None

    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            unrestricted.update(aliases)

    for column in select.find_all(exp.Column):
        alias = column.table or only_alias
        if alias is None or alias not in aliases:
            # An unqualified column with several tables in play, or a
            # qualifier we do not know: stop guessing and read everything.
            unrestricted.update(aliases)
            continue
        if isinstance(column.this, exp.Star):
            unrestricted.add(alias)
            continue
        found[alias].add(column.name)

    return {
        alias: None if alias in unrestricted or not names else frozenset(names)
        for alias, names in found.items()
    }


def _conjuncts(node: exp.Expr | None) -> Iterator[exp.Expr]:
    """Flatten an AND tree; anything else is yielded as a single condition."""
    if node is None:
        return
    if isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    elif isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    else:
        yield node


def _where_predicates(
    select: exp.Select, aliases: Mapping[str, TableRef], null_side: set[str]
) -> tuple[dict[str, list[Predicate]], list[JoinEquality]]:
    """Pushable WHERE conditions per alias, plus equalities written in the WHERE."""
    predicates: dict[str, list[Predicate]] = {}
    joins: list[JoinEquality] = []
    where = select.args.get("where")
    if where is None:
        return predicates, joins
    for condition in _conjuncts(where.this):
        _collect(condition, aliases, null_side, predicates, joins, inner=True)
    return predicates, joins


def _join_conditions(
    select: exp.Select,
    aliases: Mapping[str, TableRef],
    null_side: set[str],
    predicates: dict[str, list[Predicate]],
    joins: list[JoinEquality],
) -> None:
    """ON conditions: equalities always, single-table filters only for inner joins."""
    for join in select.args.get("joins") or []:
        inner = not (join.side or "")
        for condition in _conjuncts(join.args.get("on")):
            _collect(condition, aliases, null_side, predicates, joins, inner=inner)


def _collect(
    condition: exp.Expr,
    aliases: Mapping[str, TableRef],
    null_side: set[str],
    predicates: dict[str, list[Predicate]],
    joins: list[JoinEquality],
    inner: bool,
) -> None:
    """Sort one condition into a join equality, a pushable predicate, or neither."""
    equality = _join_equality(condition, aliases, inner)
    if equality is not None:
        joins.append(equality)
        return
    if not inner:
        return
    parsed = _predicate(condition, aliases)
    if parsed is None:
        return
    alias, predicate = parsed
    if alias in null_side:
        return
    predicates.setdefault(alias, []).append(predicate)


def _join_equality(
    condition: exp.Expr, aliases: Mapping[str, TableRef], inner: bool
) -> JoinEquality | None:
    if not isinstance(condition, exp.EQ):
        return None
    left, right = condition.this, condition.expression
    if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
        return None
    if left.table not in aliases or right.table not in aliases:
        return None
    if left.table == right.table:
        return None
    return JoinEquality(left.table, left.name, right.table, right.name, inner=inner)


def _predicate(
    condition: exp.Expr, aliases: Mapping[str, TableRef]
) -> tuple[str, Predicate] | None:
    """``(alias, Predicate)`` when the condition filters exactly one known table."""
    if isinstance(condition, exp.Is):
        column = _single_column(condition.this, aliases)
        if column is None or not isinstance(condition.expression, exp.Null):
            return None
        return column[0], Predicate(column[1], "is_null", True)

    if isinstance(condition, exp.In):
        if condition.args.get("query") or not condition.expressions:
            return None
        column = _single_column(condition.this, aliases)
        if column is None:
            return None
        values = [_literal(v) for v in condition.expressions]
        if any(v is _NOT_LITERAL for v in values):
            return None
        return column[0], Predicate(column[1], "in", tuple(values))

    if isinstance(condition, exp.Like):
        column = _single_column(condition.this, aliases)
        value = _literal(condition.expression)
        if column is None or not isinstance(value, str):
            return None
        return column[0], Predicate(column[1], "like", value)

    op = _COMPARISONS.get(type(condition))
    if op is None:
        return None
    column = _single_column(condition.this, aliases)
    value = _literal(condition.expression)
    if column is None or value is _NOT_LITERAL:
        # Try the mirrored form (``'x' = status``) before giving up.
        column = _single_column(condition.expression, aliases)
        value = _literal(condition.this)
        if column is None or value is _NOT_LITERAL:
            return None
        op = _FLIPPED[op]
    return column[0], Predicate(column[1], op, value)


def _single_column(node: exp.Expr, aliases: Mapping[str, TableRef]) -> tuple[str, str] | None:
    """``(alias, column)`` for a plain column reference of a known table."""
    if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
        return None
    alias = node.table or (next(iter(aliases)) if len(aliases) == 1 else "")
    if alias not in aliases:
        return None
    return alias, node.name


class _NotLiteral:
    """Sentinel: the expression is not a literal (``None`` is a valid value)."""

    def __repr__(self) -> str:
        return "<not a literal>"


_NOT_LITERAL = _NotLiteral()


def _literal(node: exp.Expr) -> Any:
    """Python value of a literal node, or ``_NOT_LITERAL``."""
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Neg):
        inner = _literal(node.this)
        if isinstance(inner, bool) or not isinstance(inner, int | float):
            return _NOT_LITERAL
        return -inner
    if isinstance(node, exp.Paren):
        return _literal(node.this)
    if not isinstance(node, exp.Literal):
        return _NOT_LITERAL
    text = str(node.this)
    if node.is_string:
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return _NOT_LITERAL


def _is_aggregated(select: exp.Select) -> bool:
    """GROUP BY, HAVING, DISTINCT or a bare aggregate in the projection."""
    if select.args.get("group") or select.args.get("having") or select.args.get("distinct"):
        return True
    return any(isinstance(node, exp.AggFunc) for node in select.find_all(exp.AggFunc))


def _limit_of(select: exp.Select) -> int | None:
    limit = select.args.get("limit")
    if limit is None or limit.expression is None:
        return None
    if select.args.get("offset") or select.find(exp.TableSample):
        # The skipped or sampled rows have to exist locally to be skipped.
        return None
    options = limit.args.get("limit_options")
    if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
        # ``LIMIT 10 PERCENT`` is not ten rows, and WITH TIES may need more.
        return None
    value = _literal(limit.expression)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
