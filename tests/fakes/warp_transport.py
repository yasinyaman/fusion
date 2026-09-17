"""FakeWarpTransport: an in-memory Warp REST API behind the HttpTransport port.

Serves ``/health``, ``/info``, paginated table pages and a tiny SQL
interpreter for ``/query/execute`` that understands the statements Fusion's
tools push down (COUNT(*), GROUP BY aggregates, simple WHERE, LIMIT).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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
# Transport
# ---------------------------------------------------------------------------


class FakeWarpTransport:
    """HttpTransport that answers like a Warp server holding ``db``."""

    def __init__(
        self,
        db: Mapping[str, list[dict[str, Any]]],
        database: str = "ecommerce",
        *,
        health_ok: bool = True,
        info: Any | None = None,
        page_format: str = "list",
    ) -> None:
        self.db = {k: list(v) for k, v in db.items()}
        self.database = database
        self.health_ok = health_ok
        self.info_override = info
        self.page_format = page_format
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: list[HttpTransportError] = []
        self.closed = False

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
            return {
                "databases": {self.database: {"tables": list(self.db), "table_count": len(self.db)}}
            }
        table = url.rstrip("/").rsplit("/", 1)[-1]
        if table in self.db:
            offset = int((params or {}).get("offset", 0))
            limit = int((params or {}).get("limit", 1000))
            page = self.db[table][offset : offset + limit]
            if self.page_format == "data":
                return {"data": page, "total": len(self.db[table])}
            return page
        raise HttpTransportError(f"HTTP 404 from {url}", status=404)

    def post_json(self, url: str, payload: Mapping[str, Any], timeout: float | None = None) -> Any:
        self.requests.append(("POST", url, dict(payload)))
        self._maybe_fail()
        if "/query/execute" not in url:
            raise HttpTransportError(f"HTTP 404 from {url}", status=404)
        try:
            return {"data": run_mock_sql(str(payload.get("query", "")), self.db)}
        except ValueError as e:
            raise HttpTransportError(f"HTTP 400 from {url}: {e}", status=400) from e

    def close(self) -> None:
        self.closed = True
