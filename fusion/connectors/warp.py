"""Warp REST API connector for Fusion.

Connects to a running Warp instance (https://github.com/yasinyaman/warp) to
fetch data from PostgreSQL/MySQL databases via its auto-discovery REST API.
"""

import ipaddress
import logging
import socket
from typing import Any, Optional
from urllib.parse import urlparse

import pandas as pd
import requests

from fusion.connectors.base import BaseConnector
from fusion.exceptions import ConnectionError, QueryError

logger = logging.getLogger(__name__)

# Default pagination limit per request
DEFAULT_PAGE_SIZE = 1000
DEFAULT_TIMEOUT = 30

# Cloud metadata hostnames that must never be contacted (SSRF targets)
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})


def _is_blocked_ip(host: str) -> bool:
    """True for link-local / cloud-metadata addresses (e.g. 169.254.169.254).

    Loopback and private ranges are intentionally NOT blocked — Warp normally
    runs on localhost or a private Docker network.
    """
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False


def _validate_base_url(url: str) -> None:
    """Reject non-http(s) schemes and cloud-metadata/link-local hosts.

    base_url is operator-controlled, so this is defense-in-depth against an
    accidental or hostile metadata-endpoint URL rather than user-driven SSRF.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConnectionError(
            f"Unsupported Warp URL scheme '{parsed.scheme}'. "
            f"Only http/https are allowed: {url}"
        )
    host = parsed.hostname
    if not host:
        raise ConnectionError(f"Invalid Warp URL (no host): {url}")
    if host.lower() in _BLOCKED_HOSTNAMES or _is_blocked_ip(host):
        raise ConnectionError(
            f"Blocked Warp host '{host}' (cloud metadata / link-local address)."
        )
    # Best-effort: block hostnames that resolve to a link-local/metadata address.
    try:
        resolved = socket.gethostbyname(host)
    except OSError:
        resolved = None
    if resolved and _is_blocked_ip(resolved):
        raise ConnectionError(
            f"Blocked Warp host '{host}' (resolves to link-local address {resolved})."
        )


class WarpConnector(BaseConnector):
    """Connector that fetches data from a Warp REST API instance.

    Config keys:
        type: "warp"
        base_url: Warp server URL (e.g. "http://localhost:8080")
        api_key: Optional API key for authentication
        database: Database name registered in Warp
        timeout: Request timeout in seconds (default 30)
        page_size: Rows per page for pagination (default 10000)
    """

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._base_url = config.get("base_url", "http://localhost:8080").rstrip("/")
        _validate_base_url(self._base_url)
        self._api_key = config.get("api_key")
        self._database = config.get("database", name)
        self._timeout = config.get("timeout", DEFAULT_TIMEOUT)
        self._page_size = config.get("page_size", DEFAULT_PAGE_SIZE)
        self._session: Optional[requests.Session] = None
        self._tables: list[str] = []

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            # Do not follow redirects: a 3xx to an attacker-controlled or
            # internal address (SSRF) raises TooManyRedirects instead of being
            # silently fetched.
            self._session.max_redirects = 0
            if self._api_key:
                self._session.headers["Authorization"] = f"Bearer {self._api_key}"
            self._session.headers["Accept"] = "application/json"
        return self._session

    def connect(self) -> None:
        """Connect to Warp and discover available tables."""
        session = self._get_session()

        # Health check
        try:
            resp = session.get(
                f"{self._base_url}/health",
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.ConnectionError as e:
            raise ConnectionError(
                f"Cannot connect to Warp at {self._base_url}: {e}"
            ) from e
        except requests.HTTPError as e:
            raise ConnectionError(
                f"Warp health check failed: {e}"
            ) from e

        # Discover tables via /info endpoint
        try:
            resp = session.get(
                f"{self._base_url}/info",
                timeout=self._timeout,
            )
            resp.raise_for_status()
            info = resp.json()

            # Warp /info returns database and table information
            self._tables = self._extract_tables(info)
        except requests.RequestException as e:
            raise ConnectionError(
                f"Failed to discover tables from Warp: {e}"
            ) from e

        self._connected = True
        logger.info(
            "Connected to Warp at %s (database=%s, tables=%d)",
            self._base_url, self._database, len(self._tables),
        )

    def _extract_tables(self, info: Any) -> list[str]:
        """Extract table names from Warp /info response."""
        tables = []

        if isinstance(info, dict):
            # Try common response formats
            if "tables" in info:
                raw = info["tables"]
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, str):
                            tables.append(item)
                        elif isinstance(item, dict) and "name" in item:
                            tables.append(item["name"])
            elif "databases" in info:
                dbs = info["databases"]
                # Format A (Warp actual): dict keyed by db name
                # e.g. {"primary_db": {"tables": ["t1", "t2"], "table_count": 2}}
                if isinstance(dbs, dict):
                    for db_name, db_info in dbs.items():
                        if db_name == self._database or not self._database:
                            raw_tables = (
                                db_info.get("tables", [])
                                if isinstance(db_info, dict)
                                else []
                            )
                            for t in raw_tables:
                                if isinstance(t, str):
                                    tables.append(t)
                                elif isinstance(t, dict) and "name" in t:
                                    tables.append(t["name"])
                # Format B: list of db objects
                # e.g. [{"name": "mydb", "tables": ["t1", "t2"]}]
                elif isinstance(dbs, list):
                    for db in dbs:
                        if isinstance(db, dict):
                            db_name = db.get("name", "")
                            if db_name == self._database or not self._database:
                                for t in db.get("tables", []):
                                    if isinstance(t, str):
                                        tables.append(t)
                                    elif isinstance(t, dict) and "name" in t:
                                        tables.append(t["name"])
        elif isinstance(info, list):
            # Direct list of table names or objects
            for item in info:
                if isinstance(item, str):
                    tables.append(item)
                elif isinstance(item, dict) and "name" in item:
                    tables.append(item["name"])

        return tables

    def fetch_data(self, table: str, max_rows: Optional[int] = None) -> pd.DataFrame:
        """Fetch rows from a table via Warp REST API with pagination.

        Stops early once ``max_rows`` rows have been collected (None = no cap).
        """
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        session = self._get_session()
        all_rows: list[dict] = []
        offset = 0

        while True:
            # Never request more than we still need.
            page_size = self._page_size
            if max_rows is not None:
                page_size = min(page_size, max_rows - len(all_rows))
            url = f"{self._base_url}/api/v1/{self._database}/{table}"
            params = {"limit": page_size, "offset": offset}

            try:
                resp = session.get(url, params=params, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                raise QueryError(
                    f"Failed to fetch data from {table}: {e}"
                ) from e

            # Handle different response formats
            rows = self._extract_rows(data)
            if not rows:
                break

            all_rows.extend(rows)

            # Stop if we have reached the ingest cap (and log the truncation).
            if max_rows is not None and len(all_rows) >= max_rows:
                all_rows = all_rows[:max_rows]
                logger.warning(
                    "Table %s truncated to max_rows=%d during ingest", table, max_rows
                )
                break

            # If we got fewer rows than page size, we're done
            if len(rows) < self._page_size:
                break

            offset += self._page_size

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        logger.info("Fetched %d rows from %s", len(df), table)
        return df

    def _extract_rows(self, data: Any) -> list[dict]:
        """Extract row data from API response."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Common patterns: {"data": [...]}, {"rows": [...]}, {"results": [...]}
            for key in ("data", "rows", "results", "items", "records"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    def get_schema(self) -> dict[str, dict]:
        """Infer schema by fetching a small sample from each table."""
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        schema = {}
        session = self._get_session()

        for table in self._tables:
            try:
                # Fetch a small sample to infer types
                resp = session.get(
                    f"{self._base_url}/api/v1/{self._database}/{table}",
                    params={"limit": 5},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                rows = self._extract_rows(data)

                if rows:
                    sample_df = pd.DataFrame(rows)
                    columns = []
                    for col_name in sample_df.columns:
                        dtype = str(sample_df[col_name].dtype)
                        col_type = self._pandas_to_sql_type(dtype)
                        has_nulls = sample_df[col_name].isna().any()
                        columns.append({
                            "name": col_name,
                            "type": col_type,
                            "nullable": bool(has_nulls),
                        })
                    schema[table] = {
                        "columns": columns,
                        "row_count": self._get_row_count(table, data),
                    }
                else:
                    schema[table] = {"columns": [], "row_count": 0}

            except Exception as e:
                logger.warning("Failed to get schema for table %s: %s", table, e)
                schema[table] = {"columns": [], "row_count": 0}

        return schema

    def _get_row_count(self, table: str, data: Any) -> int:
        """Try to get total row count from response metadata."""
        if isinstance(data, dict):
            for key in ("total", "count", "total_count", "total_rows"):
                if key in data and isinstance(data[key], int):
                    return data[key]
        # If no metadata, return -1 (unknown)
        return -1

    @staticmethod
    def _pandas_to_sql_type(dtype: str) -> str:
        """Map pandas dtype to SQL type name."""
        dtype = dtype.lower()
        if "int" in dtype:
            return "integer"
        if "float" in dtype or "double" in dtype:
            return "double"
        if "bool" in dtype:
            return "boolean"
        if "datetime" in dtype or "timestamp" in dtype:
            return "timestamp"
        if "date" in dtype:
            return "date"
        return "varchar"

    def fetch_data_filtered(
        self,
        table: str,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Fetch data with server-side filtering via Warp's query endpoint.

        Pushes a SQL query to Warp's backend database for filtering.
        Falls back to in-memory filtering if server-side query fails.
        """
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        col_clause = ", ".join(columns) if columns else "*"
        where_parts = []
        if filters:
            for col, val in filters.items():
                if isinstance(val, str):
                    escaped = val.replace("'", "''")
                    where_parts.append(f"{col} = '{escaped}'")
                else:
                    where_parts.append(f"{col} = {val}")

        sql = f"SELECT {col_clause} FROM {table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if limit:
            sql += f" LIMIT {limit}"

        try:
            return self.execute_query(sql)
        except Exception:
            logger.warning(
                "execute_query failed for %s, falling back to full fetch", table
            )
            return super().fetch_data_filtered(
                table, filters=filters, columns=columns, limit=limit
            )

    def execute_query(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        """Execute a SQL query via Warp's query endpoint.

        This sends the SQL to Warp's backend database (not DuckDB).
        """
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        session = self._get_session()
        payload: dict[str, Any] = {"query": sql}
        if params:
            payload["params"] = params

        try:
            resp = session.post(
                f"{self._base_url}/api/v1/{self._database}/query/execute",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise QueryError(f"Warp query execution failed: {e}") from e

        rows = self._extract_rows(data)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    @staticmethod
    def discover_databases(
        base_url: str,
        api_key: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> list[str]:
        """Discover all database names from a Warp instance's /info endpoint.

        Connects to Warp, fetches /info, and extracts database names.
        This is a static method because it runs BEFORE any WarpConnector
        instance is created (to determine which databases to connect to).

        Args:
            base_url: Warp server URL (e.g. "http://localhost:8080")
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds

        Returns:
            List of database names (e.g. ["mydb", "analytics"]).
            Empty list if /info response has no "databases" key.
        """
        base_url = base_url.rstrip("/")
        _validate_base_url(base_url)
        session = requests.Session()
        session.max_redirects = 0  # don't follow redirects (SSRF guard)
        if api_key:
            session.headers["Authorization"] = f"Bearer {api_key}"
        session.headers["Accept"] = "application/json"

        try:
            # Health check
            resp = session.get(f"{base_url}/health", timeout=timeout)
            resp.raise_for_status()

            # Discover via /info
            resp = session.get(f"{base_url}/info", timeout=timeout)
            resp.raise_for_status()
            info = resp.json()
        except requests.ConnectionError as e:
            raise ConnectionError(
                f"Cannot connect to Warp at {base_url}: {e}"
            ) from e
        except requests.RequestException as e:
            raise ConnectionError(
                f"Failed to discover databases from Warp: {e}"
            ) from e
        finally:
            session.close()

        # Extract database names from response
        databases: list[str] = []
        if isinstance(info, dict) and "databases" in info:
            dbs = info["databases"]
            # Format A (Warp actual): dict keyed by db name
            # e.g. {"primary_db": {"tables": [...]}, "mysql_db": {...}}
            if isinstance(dbs, dict):
                databases.extend(dbs.keys())
            # Format B: list of db objects or strings
            # e.g. [{"name": "db1"}, ...] or ["db1", "db2"]
            elif isinstance(dbs, list):
                for db in dbs:
                    if isinstance(db, dict) and "name" in db:
                        databases.append(db["name"])
                    elif isinstance(db, str):
                        databases.append(db)

        return databases

    @property
    def supports_pushdown(self) -> bool:
        """Warp supports query pushdown via execute_query() endpoint."""
        return True

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None
        self._connected = False
        logger.info("WarpConnector closed for %s", self.name)
