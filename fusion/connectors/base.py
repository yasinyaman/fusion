"""Abstract base class for all data source connectors."""

from abc import ABC, abstractmethod

import pandas as pd


class BaseConnector(ABC):
    """Abstract base connector that all source connectors must implement."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._connected = False

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the data source."""

    @abstractmethod
    def fetch_data(self, table: str, max_rows: int | None = None) -> pd.DataFrame:
        """Fetch data from a table/collection as a DataFrame.

        Args:
            table: Table name.
            max_rows: Stop after this many rows (None = no limit). Guards against
                pulling an unbounded result set into memory.
        """

    @abstractmethod
    def get_schema(self) -> dict[str, dict]:
        """Return schema info for all tables.

        Returns:
            Dict of table_name -> {
                "columns": [{"name": str, "type": str, "nullable": bool}, ...]
            }
        """

    @abstractmethod
    def close(self) -> None:
        """Close the connection to the data source."""

    def fetch_data_filtered(
        self,
        table: str,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
    ) -> "pd.DataFrame":
        """Fetch data with optional filtering.

        Default implementation fetches all data and filters in-memory with pandas.
        Subclasses can override for server-side filtering.

        Args:
            table: Table name
            filters: Dict of {column: value} for equality filters
            columns: Column names to select (None = all)
            limit: Max rows to fetch
        """
        df = self.fetch_data(table, max_rows=limit)
        if filters:
            for col, val in filters.items():
                if col in df.columns:
                    df = df[df[col] == val]
        if columns:
            valid_cols = [c for c in columns if c in df.columns]
            if valid_cols:
                df = df[valid_cols]
        if limit:
            df = df.head(limit)
        return df

    def test_connection(self) -> bool:
        """Test if connection to the data source is working."""
        try:
            self.connect()
            return True
        except Exception:
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def supports_pushdown(self) -> bool:
        """Whether this connector supports query pushdown to the source database.

        Connectors that can execute SQL directly on the source (e.g. via
        execute_query) should override this to return True.
        """
        return False
