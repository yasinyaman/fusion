"""Conversions between domain results and pandas / Arrow (both optional).

pandas is not a core dependency: install ``fusion[pandas]`` to use
``to_dataframe`` / ``rowset_from_dataframe``.
"""

from __future__ import annotations

import math
from typing import Any

from fusion.domain.models import QueryResult, RowSet


def _pandas() -> Any:
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "pandas is not installed; install 'fusion[pandas]' to use DataFrame conversions"
        ) from e
    return pd


def _rows_and_columns(data: QueryResult | RowSet) -> tuple[list[str], list[tuple[Any, ...]]]:
    if isinstance(data, QueryResult):
        return list(data.columns), list(data.rows)
    return list(data.columns), list(data.rows)


def to_dataframe(data: QueryResult | RowSet) -> Any:
    """A ``pandas.DataFrame`` with the result's columns and rows."""
    pd = _pandas()
    columns, rows = _rows_and_columns(data)
    return pd.DataFrame(rows, columns=columns)


def rowset_from_dataframe(df: Any) -> RowSet:
    """A domain ``RowSet`` from a DataFrame; NaN/NaT become ``None``."""
    pd = _pandas()
    columns = tuple(str(c) for c in df.columns)
    rows = [tuple(_plain(v, pd) for v in row) for row in df.itertuples(index=False, name=None)]
    return RowSet(columns=columns, rows=rows)


def to_arrow(data: QueryResult | RowSet) -> Any:
    """A ``pyarrow.Table`` (pyarrow is a core dependency)."""
    import pyarrow as pa

    columns, rows = _rows_and_columns(data)
    return pa.Table.from_pylist([dict(zip(columns, row, strict=True)) for row in rows])


def _plain(value: Any, pd: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isnan(value):
        return None
    item = getattr(value, "item", None)  # numpy scalars -> Python scalars
    if callable(item) and not isinstance(value, (str, bytes)):
        try:
            return item()
        except (TypeError, ValueError):
            return value
    return value
