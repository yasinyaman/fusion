"""FakeWarpTransport: an in-memory Warp REST API behind the HttpTransport port.

Serves ``/health``, ``/info``, the typed ``/schema`` endpoints, paginated
table pages with Warp's ``filter[col][op]`` syntax, the streaming
``/{table}/export`` endpoint (JSON, NDJSON and real Arrow IPC) and a tiny SQL
interpreter for ``/query/execute`` that understands the statements Fusion's
tools push down (COUNT(*), GROUP BY aggregates, simple WHERE, LIMIT).
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fusion.adapters.outbound.warp.http import HttpTransportError

# ---------------------------------------------------------------------------
# Mini SQL interpreter over list-of-dict tables
# ---------------------------------------------------------------------------


def _extract_limit(query: str) -> int | None:
    match = re.search(r"LIMIT\s+(\d+)", query, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _find_table(query: str, db: Mapping[str, list[dict[str, Any]]]) -> str | None:
    upper = query.upper()
    for table in sorted(db, key=len, reverse=True):
        if re.search(rf"\b{re.escape(table.upper())}\b", upper):
            return table
    return None


def _apply_where(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    upper = query.upper()
    where_idx = upper.find("WHERE")
    if where_idx == -1:
        return rows
    where_idx += 5
    end_idx = len(query)
    for keyword in ("GROUP BY", "ORDER BY", "LIMIT"):
        idx = upper.find(keyword, where_idx)
        if idx != -1:
            end_idx = min(end_idx, idx)
    clause = query[where_idx:end_idx].strip()

    cast_eq = re.search(r"CAST\((\w+)\s+AS\s+VARCHAR\)\s*=\s*'([^']*)'", clause, re.IGNORECASE)
    if cast_eq:
        col, val = cast_eq.groups()
        return [r for r in rows if str(r.get(col)) == val]

    cast_like = re.search(r"CAST\((\w+)\s+AS\s+VARCHAR\)\s+LIKE\s+'([^']*)'", clause, re.IGNORECASE)
    if cast_like:
        col, pattern = cast_like.groups()
        regex = "^" + re.escape(pattern).replace("%", ".*") + "$"
        return [r for r in rows if re.match(regex, str(r.get(col)), re.IGNORECASE)]

    eq = re.search(r"(\w+)\s*=\s*'([^']*)'", clause)
    if eq:
        col, val = eq.groups()
        return [r for r in rows if str(r.get(col)) == val]

    num = re.search(r"(\w+)\s*=\s*(\d+(?:\.\d+)?)", clause)
    if num:
        col, val = num.group(1), float(num.group(2))
        return [r for r in rows if r.get(col) == val]

    gt = re.search(r"(\w+)\s*>\s*(\d+(?:\.\d+)?)", clause)
    if gt:
        col, val = gt.group(1), float(gt.group(2))
        return [r for r in rows if r.get(col) is not None and r.get(col) > val]
    return rows


def _aggregate(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    gb = re.search(r"GROUP\s+BY\s+(\w+)", query, re.IGNORECASE)
    agg = re.search(
        r"(SUM|AVG|COUNT|MIN|MAX)\((\w+|\*)\)\s+(?:as|AS)\s+(\w+)", query, re.IGNORECASE
    )
    if not gb or not agg:
        return []
    group_col = gb.group(1)
    func, agg_col, alias = agg.group(1).upper(), agg.group(2), agg.group(3)

    groups: dict[Any, list[Any]] = {}
    for r in rows:
        groups.setdefault(r.get(group_col), []).append(r.get(agg_col) if agg_col != "*" else 1)

    def reduce(values: list[Any]) -> Any:
        nums = [v for v in values if v is not None]
        if func == "COUNT":
            return len(nums)
        if not nums:
            return None
        if func == "SUM":
            return sum(nums)
        if func == "AVG":
            return sum(nums) / len(nums)
        if func == "MIN":
            return min(nums)
        return max(nums)

    result = [{group_col: k, alias: reduce(v)} for k, v in groups.items()]
    if re.search(r"ORDER\s+BY\s+\w+\s+DESC", query, re.IGNORECASE):
        result.sort(key=lambda r: (r[alias] is None, r[alias]), reverse=True)
    limit = _extract_limit(query)
    return result[:limit] if limit else result


def run_mock_sql(query: str, db: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Execute a supported SQL statement against ``db`` and return records."""
    upper = query.upper().strip()
    if re.search(r"\bJOIN\b", upper):
        # The mock backend cannot join; behave like a gateway rejecting the
        # query so Fusion falls back to loading the tables locally.
        raise ValueError("mock Warp backend does not support JOIN")
    table = _find_table(query, db)
    if table is None:
        return [{"count": 0}] if "COUNT(*)" in upper else []
    rows = list(db[table])

    if "COUNT(*)" in upper and "GROUP BY" not in upper:
        return [{"count": len(_apply_where(rows, query))}]
    if "GROUP BY" in upper:
        return _aggregate(_apply_where(rows, query), query)
    filtered = _apply_where(rows, query)
    limit = _extract_limit(query)
    return filtered[:limit] if limit else filtered


# ---------------------------------------------------------------------------
# Query-parameter handling (Warp's list/export contract)
# ---------------------------------------------------------------------------

_FILTER_RE = re.compile(r"^filter\[([^\]]+)\]\[([^\]]+)\]$")

_KINDS = {
    int: "int",
    float: "float",
    bool: "bool",
    str: "str",
    datetime: "datetime",
    date: "date",
    Decimal: "float",
    bytes: "bytes",
}


def _coerce(text: str, sample: Any) -> Any:
    """Turn a query-string value into the type the column holds."""
    if isinstance(sample, bool):
        return text.strip().lower() in ("true", "1", "yes")
    if isinstance(sample, int):
        try:
            return int(text)
        except ValueError:
            return text
    if isinstance(sample, float):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _match(value: Any, op: str, wanted: Any) -> bool:
    if op == "is_null":
        return (value is None) is bool(wanted)
    if value is None:
        return False
    if op == "eq":
        return value == wanted
    if op == "ne":
        return value != wanted
    if op == "gt":
        return value > wanted
    if op == "gte":
        return value >= wanted
    if op == "lt":
        return value < wanted
    if op == "lte":
        return value <= wanted
    if op == "in":
        return value in wanted
    if op == "like":
        pattern = "^" + re.escape(str(wanted)).replace("%", ".*").replace("_", ".") + "$"
        return re.match(pattern, str(value), re.IGNORECASE) is not None
    raise HttpTransportError(f"HTTP 400: unknown operator '{op}'", status=400)


def apply_query_params(
    rows: list[dict[str, Any]], params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Filter, sort and project rows the way Warp's list endpoint does."""
    sample = rows[0] if rows else {}
    for key, raw in params.items():
        found = _FILTER_RE.match(str(key))
        if not found:
            continue
        column, op = found.groups()
        if rows and column not in sample:
            raise HttpTransportError(f"HTTP 400: unknown column '{column}'", status=400)
        text = str(raw)
        if op == "in":
            wanted: Any = [_coerce(v, sample.get(column)) for v in text.split(",")]
        elif op == "is_null":
            wanted = text.lower() in ("true", "1", "yes")
        else:
            wanted = _coerce(text, sample.get(column))
        rows = [r for r in rows if _match(r.get(column), op, wanted)]
    sort = params.get("sort")
    if sort:
        for part in reversed(str(sort).split(",")):
            raw_column, _, direction = part.partition(":")
            column = raw_column.strip()
            rows = sorted(
                rows,
                key=lambda r, c=column: (r.get(c) is None, r.get(c)),
                reverse=direction.strip().lower() == "desc",
            )
    fields = params.get("fields")
    if fields:
        keep = [f.strip() for f in str(fields).split(",") if f.strip()]
        missing = [f for f in keep if rows and f not in sample]
        if missing:
            raise HttpTransportError(f"HTTP 400: invalid fields {missing}", status=400)
        rows = [{f: r.get(f) for f in keep} for r in rows]
    return rows


def apply_body_filters(rows: list[dict[str, Any]], body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Same, for the POST export body."""
    for condition in body.get("filters", []) or []:
        column, op = condition["column"], condition.get("op", "eq")
        rows = [r for r in rows if _match(r.get(column), op, condition.get("value"))]
    sort = body.get("sort") or []
    for part in reversed(list(sort)):
        rows = sorted(
            rows,
            key=lambda r, c=part["column"]: (r.get(c) is None, r.get(c)),
            reverse=part.get("direction") == "desc",
        )
    fields = body.get("fields")
    if fields:
        rows = [{f: r.get(f) for f in fields} for r in rows]
    return rows


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def encode_rows(rows: list[dict[str, Any]], fmt: str) -> bytes:
    """Serialize rows the way Warp's export endpoint does."""
    if fmt == "ndjson":
        return "".join(json.dumps(r, default=_json_default) + "\n" for r in rows).encode("utf-8")
    if fmt == "arrow":
        import pyarrow as pa

        columns = list(rows[0]) if rows else []
        table = (
            pa.table({c: [r.get(c) for r in rows] for c in columns}) if columns else pa.table({})
        )
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return sink.getvalue()
    return json.dumps({"items": rows, "row_count": len(rows)}, default=_json_default).encode()


class FakeByteStream:
    """A ByteStream over an in-memory body (stands in for a streamed response)."""

    def __init__(self, payload: bytes, headers: Mapping[str, str] | None = None) -> None:
        self.raw = io.BytesIO(payload)
        self.headers = dict(headers or {})
        self.closed = False

    def iter_lines(self) -> Iterator[bytes]:
        for line in self.raw.read().split(b"\n"):
            if line:
                yield line

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


FILTER_OPS = ["eq", "gt", "gte", "in", "is_null", "like", "lt", "lte", "ne"]


class FakeWarpTransport:
    """HttpTransport that answers like a Warp server holding ``db``.

    ``mode="0.10"`` (default) advertises the 0.10 ``capabilities`` block;
    ``mode="legacy"`` behaves like Warp 0.9 (no capabilities). ``raw_query``
    is what ``/info`` reports; ``deny_raw_query`` makes ``/query/execute``
    answer 403 regardless (a production Warp). ``single_db_unprefixed``
    reproduces a legacy single-database Warp that serves ``/api/v1/{table}``
    and 404s the ``/api/v1/{db}/{table}`` form.
    """

    def __init__(
        self,
        db: Mapping[str, list[dict[str, Any]]],
        database: str = "ecommerce",
        *,
        health_ok: bool = True,
        info: Any | None = None,
        page_format: str = "auto",
        mode: str = "0.10",
        raw_query: bool = True,
        deny_raw_query: bool = False,
        single_db_unprefixed: bool = False,
        api_prefix: str = "/api/v1",
        export_enabled: bool = True,
        export_max_rows: int = 0,
        arrow: bool = True,
        broken_arrow: bytes | None = None,
        row_estimates: Mapping[str, int] | None = None,
    ) -> None:
        self.db = {k: list(v) for k, v in db.items()}
        self.database = database
        self.health_ok = health_ok
        self.info_override = info
        self.page_format = page_format
        self.mode = mode
        self.raw_query = raw_query
        self.deny_raw_query = deny_raw_query
        self.single_db_unprefixed = single_db_unprefixed
        self.api_prefix = api_prefix
        self.export_enabled = export_enabled
        self.export_max_rows = export_max_rows
        self.arrow = arrow
        #: Bytes to answer an Arrow export with when it must fail to decode.
        self.broken_arrow = broken_arrow
        #: Pretend a table is huge (reported by /schema) without holding rows.
        self.row_estimates = dict(row_estimates or {})
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: list[HttpTransportError] = []
        self.closed = False

    # -- /info --------------------------------------------------------------

    def info_payload(self) -> dict[str, Any]:
        legacy = self.mode == "legacy"
        payload: dict[str, Any] = {
            "name": "Warp Engine",
            "version": "0.9.0" if legacy else "0.10.0",
            "databases": {self.database: {"tables": list(self.db), "table_count": len(self.db)}},
            "settings": {"api_prefix": self.api_prefix, "raw_query_enabled": self.raw_query},
        }
        if not legacy:
            payload["capabilities"] = {
                "api_prefix": self.api_prefix,
                "db_prefix": "always",
                "schema": True,
                "export": {
                    "enabled": self.export_enabled,
                    "formats": ["json", "ndjson"] + (["arrow"] if self.arrow else []),
                    "max_rows": self.export_max_rows,
                    "batch_size": 5000,
                },
                "raw_query": self.raw_query,
                "filter_ops": FILTER_OPS,
            }
        return payload

    def _check_layout(self, url: str) -> None:
        """404 the db-prefixed form when emulating a legacy single-DB Warp."""
        if self.single_db_unprefixed and f"{self.api_prefix}/{self.database}/" in url:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)

    # -- helpers ------------------------------------------------------------

    def fail_next(self, message: str = "boom", status: int | None = 500) -> None:
        self.failures.append(HttpTransportError(message, status=status))

    def _maybe_fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def urls(self, method: str | None = None) -> list[str]:
        return [u for m, u, _ in self.requests if method is None or m == method]

    # -- HttpTransport ------------------------------------------------------

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        self.requests.append(("GET", url, dict(params or {})))
        self._maybe_fail()
        if url.endswith("/health"):
            if not self.health_ok:
                raise HttpTransportError(f"HTTP 500 from {url}", status=500)
            return {"status": "ok"}
        if url.endswith("/info"):
            if self.info_override is not None:
                return self.info_override
            return self.info_payload()
        self._check_layout(url)
        if url.endswith("/schema"):
            return self._schema_payload(url)
        table = url.rstrip("/").rsplit("/", 1)[-1]
        if table not in self.db:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        return self._page(table, params or {})

    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float | None = None) -> Any:
        self.requests.append(("POST", url, dict(payload)))
        self._maybe_fail()
        if "/query/execute" not in url:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        self._check_layout(url)
        if self.deny_raw_query or not self.raw_query:
            raise HttpTransportError(f"HTTP 403 from {url}: raw query disabled", status=403)
        try:
            return {"data": run_mock_sql(str(payload.get("query", "")), self.db)}
        except ValueError as e:
            raise HttpTransportError(f"HTTP 400 from {url}: {e}", status=400) from e

    # -- streaming export ---------------------------------------------------

    def get_stream(
        self, url: str, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> FakeByteStream:
        self.requests.append(("GET", url, dict(params or {})))
        self._maybe_fail()
        table = self._export_table(url)
        query = dict(params or {})
        rows = apply_query_params(list(self.db[table]), query)
        return self._export(rows, str(query.get("format", "json")), query.get("limit"))

    def post_stream(
        self, url: str, payload: Mapping[str, Any], timeout: float | None = None
    ) -> FakeByteStream:
        self.requests.append(("POST", url, dict(payload)))
        self._maybe_fail()
        table = self._export_table(url)
        rows = apply_body_filters(list(self.db[table]), payload)
        return self._export(rows, str(payload.get("format", "json")), payload.get("limit"))

    # -- helpers ------------------------------------------------------------

    def _page(self, table: str, params: Mapping[str, Any]) -> Any:
        rows = apply_query_params(list(self.db[table]), params)
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 1000))
        page = rows[offset : offset + limit]
        if self.page_format == "data":
            return {"data": page, "total": len(rows)}
        if self.page_format == "list" or (self.page_format == "auto" and self.mode == "legacy"):
            return page
        return {"items": page, "total": len(rows), "limit": limit, "offset": offset}

    def _export_table(self, url: str) -> str:
        if not url.endswith("/export"):
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        if self.mode == "legacy" or not self.export_enabled:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        self._check_layout(url)
        table = url[: -len("/export")].rstrip("/").rsplit("/", 1)[-1]
        if table not in self.db:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        return table

    def _export(self, rows: list[dict[str, Any]], fmt: str, limit: Any) -> FakeByteStream:
        if fmt == "arrow" and not self.arrow:
            raise HttpTransportError("HTTP 501: pyarrow is not installed", status=501)
        headers = {"X-Export-Format": fmt}
        caps = [int(v) for v in (limit, self.export_max_rows or None) if v is not None]
        if self.export_max_rows and (limit is None or int(limit) > self.export_max_rows):
            headers["X-Export-Max-Rows"] = str(self.export_max_rows)
        if caps:
            rows = rows[: min(caps)]
        payload = self.broken_arrow if fmt == "arrow" and self.broken_arrow else None
        return FakeByteStream(payload or encode_rows(rows, fmt), headers)

    def _schema_payload(self, url: str) -> Any:
        if self.mode == "legacy":
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        rest = url[: -len("/schema")].rstrip("/")
        table = rest.rsplit("/", 1)[-1]
        if table in self.db:
            return self._table_schema(table)
        return {
            "database": self.database,
            "tables": {name: self._table_schema(name) for name in self.db},
        }

    def _table_schema(self, table: str) -> dict[str, Any]:
        rows = self.db[table]
        columns: dict[str, str] = {}
        for row in rows[:20]:
            for key, value in row.items():
                if key not in columns or columns[key] == "str":
                    columns[key] = _KINDS.get(type(value), "str") if value is not None else "str"
        estimate = self.row_estimates.get(table, len(rows))
        return {
            "table": table,
            "columns": [
                {
                    "name": name,
                    "type": kind,
                    "kind": kind,
                    "nullable": any(r.get(name) is None for r in rows),
                }
                for name, kind in columns.items()
            ],
            "primary_key": ["id"] if columns.get("id") else [],
            "row_estimate": estimate,
        }

    def close(self) -> None:
        self.closed = True
