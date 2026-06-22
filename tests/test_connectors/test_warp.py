"""Tests for WarpConnector (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from fusion.connectors.warp import WarpConnector, _validate_base_url
from fusion.exceptions import ConnectionError, QueryError


@pytest.fixture
def warp_config():
    return {
        "type": "warp",
        "base_url": "http://localhost:8080",
        "database": "testdb",
    }


@pytest.fixture
def mock_session():
    """Create a mock requests.Session."""
    with patch("fusion.connectors.warp.requests.Session") as MockSession:
        session = MagicMock()
        MockSession.return_value = session
        yield session


class TestWarpConnector:
    def test_init(self, warp_config):
        conn = WarpConnector("test", warp_config)
        assert conn.name == "test"
        assert conn._base_url == "http://localhost:8080"
        assert conn._database == "testdb"
        assert not conn.is_connected

    def test_connect_success(self, warp_config, mock_session):
        health_resp = MagicMock()
        health_resp.status_code = 200
        health_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {"tables": ["users", "orders"]}

        mock_session.get.side_effect = [health_resp, info_resp]

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn.connect()

        assert conn.is_connected
        assert conn._tables == ["users", "orders"]

    def test_connect_failure(self, warp_config, mock_session):
        mock_session.get.side_effect = requests.ConnectionError("refused")

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session

        with pytest.raises(ConnectionError, match="Cannot connect"):
            conn.connect()

    def test_connect_health_check_failure(self, warp_config, mock_session):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        mock_session.get.return_value = resp

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session

        with pytest.raises(ConnectionError, match="health check failed"):
            conn.connect()

    def test_fetch_data(self, warp_config, mock_session):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]
        mock_session.get.return_value = resp

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True
        conn._tables = ["users"]

        df = conn.fetch_data("users")
        assert len(df) == 2
        assert "name" in df.columns
        assert df.iloc[0]["name"] == "Alice"

    def test_fetch_data_with_data_wrapper(self, warp_config, mock_session):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": [{"id": 1, "val": 10}, {"id": 2, "val": 20}],
            "total": 2,
        }
        mock_session.get.return_value = resp

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True

        df = conn.fetch_data("items")
        assert len(df) == 2

    def test_fetch_data_pagination(self, warp_config, mock_session):
        config = {**warp_config, "page_size": 2}

        page1 = MagicMock()
        page1.status_code = 200
        page1.raise_for_status = MagicMock()
        page1.json.return_value = [{"id": 1}, {"id": 2}]

        page2 = MagicMock()
        page2.status_code = 200
        page2.raise_for_status = MagicMock()
        page2.json.return_value = [{"id": 3}]

        mock_session.get.side_effect = [page1, page2]

        conn = WarpConnector("test", config)
        conn._session = mock_session
        conn._connected = True

        df = conn.fetch_data("items")
        assert len(df) == 3

    def test_fetch_data_respects_max_rows(self, warp_config, mock_session):
        config = {**warp_config, "page_size": 2}

        page1 = MagicMock()
        page1.raise_for_status = MagicMock()
        page1.json.return_value = [{"id": 1}, {"id": 2}]

        page2 = MagicMock()
        page2.raise_for_status = MagicMock()
        page2.json.return_value = [{"id": 3}, {"id": 4}]

        mock_session.get.side_effect = [page1, page2]

        conn = WarpConnector("test", config)
        conn._session = mock_session
        conn._connected = True

        df = conn.fetch_data("items", max_rows=3)
        assert len(df) == 3  # truncated at the ingest cap

    def test_fetch_data_not_connected(self, warp_config):
        conn = WarpConnector("test", warp_config)
        with pytest.raises(ConnectionError, match="Not connected"):
            conn.fetch_data("users")

    def test_get_schema(self, warp_config, mock_session):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [
            {"id": 1, "name": "Alice", "score": 95.5},
        ]
        mock_session.get.return_value = resp

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True
        conn._tables = ["users"]

        schema = conn.get_schema()
        assert "users" in schema
        col_names = [c["name"] for c in schema["users"]["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "score" in col_names

    def test_execute_query(self, warp_config, mock_session):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": [{"count": 42}]}
        mock_session.post.return_value = resp

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True

        df = conn.execute_query("SELECT COUNT(*) as count FROM users")
        assert len(df) == 1
        assert df.iloc[0]["count"] == 42

    def test_execute_query_failure(self, warp_config, mock_session):
        mock_session.post.side_effect = requests.RequestException("query failed")

        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True

        with pytest.raises(QueryError, match="query failed"):
            conn.execute_query("SELECT 1")

    def test_close(self, warp_config, mock_session):
        conn = WarpConnector("test", warp_config)
        conn._session = mock_session
        conn._connected = True

        conn.close()
        assert not conn.is_connected
        mock_session.close.assert_called_once()

    def test_extract_tables_dict_with_tables(self, warp_config):
        conn = WarpConnector("test", warp_config)
        tables = conn._extract_tables({"tables": ["t1", "t2"]})
        assert tables == ["t1", "t2"]

    def test_extract_tables_dict_with_objects(self, warp_config):
        conn = WarpConnector("test", warp_config)
        tables = conn._extract_tables({
            "tables": [{"name": "t1"}, {"name": "t2"}]
        })
        assert tables == ["t1", "t2"]

    def test_extract_tables_list(self, warp_config):
        conn = WarpConnector("test", warp_config)
        tables = conn._extract_tables(["t1", "t2"])
        assert tables == ["t1", "t2"]

    def test_extract_tables_databases_list_format(self, warp_config):
        conn = WarpConnector("test", warp_config)
        info = {
            "databases": [
                {"name": "testdb", "tables": ["users", "orders"]},
                {"name": "other", "tables": ["logs"]},
            ]
        }
        tables = conn._extract_tables(info)
        assert "users" in tables
        assert "orders" in tables
        assert "logs" not in tables

    def test_extract_tables_databases_dict_format(self, warp_config):
        """Warp actual format: databases as dict keyed by db name."""
        conn = WarpConnector("test", warp_config)
        info = {
            "databases": {
                "testdb": {
                    "tables": ["categories", "orders", "users"],
                    "table_count": 3,
                },
                "other_db": {
                    "tables": ["logs", "events"],
                    "table_count": 2,
                },
            }
        }
        tables = conn._extract_tables(info)
        assert "categories" in tables
        assert "orders" in tables
        assert "users" in tables
        # other_db tables should NOT be included (database filter = "testdb")
        assert "logs" not in tables
        assert "events" not in tables

    def test_extract_tables_databases_dict_no_filter(self, warp_config):
        """When database is empty, all databases' tables are included."""
        config = {**warp_config, "database": ""}
        conn = WarpConnector("test", config)
        conn._database = ""  # Override to empty
        info = {
            "databases": {
                "db1": {"tables": ["t1", "t2"]},
                "db2": {"tables": ["t3"]},
            }
        }
        tables = conn._extract_tables(info)
        assert tables == ["t1", "t2", "t3"]

    def test_pandas_to_sql_type(self):
        assert WarpConnector._pandas_to_sql_type("int64") == "integer"
        assert WarpConnector._pandas_to_sql_type("float64") == "double"
        assert WarpConnector._pandas_to_sql_type("bool") == "boolean"
        assert WarpConnector._pandas_to_sql_type("datetime64[ns]") == "timestamp"
        assert WarpConnector._pandas_to_sql_type("object") == "varchar"


class TestDiscoverDatabases:
    """Tests for WarpConnector.discover_databases() static method."""

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_list_format(self, MockSession):
        """Discovers databases from {"databases": [{"name": ...}, ...]} format."""
        session = MagicMock()
        MockSession.return_value = session

        health_resp = MagicMock()
        health_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {
            "databases": [
                {"name": "mydb", "tables": ["users", "orders"]},
                {"name": "analytics", "tables": ["events", "metrics"]},
            ]
        }
        session.get.side_effect = [health_resp, info_resp]

        databases = WarpConnector.discover_databases("http://localhost:8080")
        assert databases == ["mydb", "analytics"]
        session.close.assert_called_once()

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_dict_keys_format(self, MockSession):
        """Warp actual format: databases as dict keyed by db name."""
        session = MagicMock()
        MockSession.return_value = session

        health_resp = MagicMock()
        health_resp.raise_for_status = MagicMock()

        info_resp = MagicMock()
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {
            "databases": {
                "primary_db": {
                    "tables": ["categories", "orders", "users"],
                    "table_count": 3,
                },
                "mysql_db": {
                    "tables": ["events", "metrics"],
                    "table_count": 2,
                },
            }
        }
        session.get.side_effect = [health_resp, info_resp]

        databases = WarpConnector.discover_databases("http://localhost:8080")
        assert "primary_db" in databases
        assert "mysql_db" in databases
        assert len(databases) == 2

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_string_format(self, MockSession):
        """Discovers databases from {"databases": ["db1", "db2"]} format."""
        session = MagicMock()
        MockSession.return_value = session

        health_resp = MagicMock()
        health_resp.raise_for_status = MagicMock()
        info_resp = MagicMock()
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {"databases": ["db1", "db2"]}
        session.get.side_effect = [health_resp, info_resp]

        databases = WarpConnector.discover_databases("http://localhost:8080")
        assert databases == ["db1", "db2"]

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_no_databases_key(self, MockSession):
        """Returns empty list when /info has no 'databases' key."""
        session = MagicMock()
        MockSession.return_value = session

        health_resp = MagicMock()
        health_resp.raise_for_status = MagicMock()
        info_resp = MagicMock()
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {"tables": ["t1", "t2"]}
        session.get.side_effect = [health_resp, info_resp]

        databases = WarpConnector.discover_databases("http://localhost:8080")
        assert databases == []

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_connection_error(self, MockSession):
        """Raises ConnectionError when Warp is unreachable."""
        session = MagicMock()
        MockSession.return_value = session
        session.get.side_effect = requests.ConnectionError("refused")

        with pytest.raises(ConnectionError, match="Cannot connect"):
            WarpConnector.discover_databases("http://localhost:8080")

    @patch("fusion.connectors.warp.requests.Session")
    def test_discover_databases_with_api_key(self, MockSession):
        """Sets Authorization header when api_key is provided."""
        session = MagicMock()
        session.headers = {}
        MockSession.return_value = session

        health_resp = MagicMock()
        health_resp.raise_for_status = MagicMock()
        info_resp = MagicMock()
        info_resp.raise_for_status = MagicMock()
        info_resp.json.return_value = {"databases": [{"name": "db1"}]}
        session.get.side_effect = [health_resp, info_resp]

        WarpConnector.discover_databases("http://localhost:8080", api_key="secret")
        assert session.headers["Authorization"] == "Bearer secret"


class TestWarpSSRFGuard:
    """SSRF defense-in-depth on the operator-supplied base_url."""

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ConnectionError):
            _validate_base_url("file:///etc/passwd")
        with pytest.raises(ConnectionError):
            _validate_base_url("ftp://example.com")

    def test_rejects_metadata_ip(self):
        with pytest.raises(ConnectionError):
            _validate_base_url("http://169.254.169.254")

    def test_rejects_metadata_hostname(self):
        with pytest.raises(ConnectionError):
            _validate_base_url("http://metadata.google.internal/computeMetadata")

    def test_allows_loopback_and_private(self):
        # Warp legitimately runs on these — must not raise
        _validate_base_url("http://localhost:8080")
        _validate_base_url("http://127.0.0.1:8000")
        _validate_base_url("http://10.0.0.5:8000")

    def test_connector_init_rejects_metadata(self):
        with pytest.raises(ConnectionError):
            WarpConnector("evil", {
                "type": "warp",
                "base_url": "http://169.254.169.254",
                "database": "x",
            })

    def test_discover_databases_rejects_metadata(self):
        with pytest.raises(ConnectionError):
            WarpConnector.discover_databases("http://169.254.169.254")

    def test_session_does_not_follow_redirects(self, warp_config, mock_session):
        conn = WarpConnector("test", warp_config)
        conn._get_session()
        assert mock_session.max_redirects == 0
