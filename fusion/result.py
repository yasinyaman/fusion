"""Query result wrapper with multiple output formats."""

import json
import time
from typing import Optional

import pandas as pd
from tabulate import tabulate


class QueryResult:
    """Wraps query results with conversion methods for various output formats."""

    def __init__(
        self,
        data: list[tuple],
        columns: list[str],
        sql: str = "",
        execution_time: float = 0.0,
        from_cache: bool = False,
    ):
        self._data = data
        self._columns = columns
        self.sql = sql
        self.execution_time = execution_time
        self.from_cache = from_cache
        self._created_at = time.time()

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    @property
    def row_count(self) -> int:
        return len(self._data)

    @property
    def column_count(self) -> int:
        return len(self._columns)

    def __len__(self) -> int:
        return self.row_count

    def __repr__(self) -> str:
        return (
            f"QueryResult(rows={self.row_count}, cols={self.column_count}, "
            f"time={self.execution_time:.1f}ms, cached={self.from_cache})"
        )

    def to_dataframe(self) -> pd.DataFrame:
        """Convert to pandas DataFrame."""
        return pd.DataFrame(self._data, columns=self._columns)

    def to_dict(self) -> list[dict]:
        """Convert to list of dictionaries."""
        return [dict(zip(self._columns, row)) for row in self._data]

    def to_markdown(self) -> str:
        """Convert to Markdown table using tabulate."""
        return tabulate(self._data, headers=self._columns, tablefmt="github")

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        records = self.to_dict()
        return json.dumps(records, indent=indent, default=str, ensure_ascii=False)

    def to_csv(self, path: Optional[str] = None) -> Optional[str]:
        """Convert to CSV. If path given, write to file; otherwise return string."""
        df = self.to_dataframe()
        if path:
            df.to_csv(path, index=False)
            return None
        return df.to_csv(index=False)

    def summary(self) -> str:
        """Generate a short summary for LLM consumption."""
        lines = [f"Query returned {self.row_count} rows, {self.column_count} columns."]
        lines.append(f"Columns: {', '.join(self._columns)}")

        if self._data:
            preview_rows = self._data[:5]
            lines.append("First rows:")
            for row in preview_rows:
                lines.append(f"  {row}")

        if self.row_count > 5:
            lines.append(f"  ... and {self.row_count - 5} more rows")

        return "\n".join(lines)
