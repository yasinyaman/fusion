"""Tests for the MCP server adapter (mcp 2.x MCPServer)."""

import asyncio
import inspect
import json

import pytest

from fusion.adapters.inbound.mcp.server import create_mcp_server
from fusion.application.tool_schemas import TOOL_NAMES


def _await(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _text(result):
    """Extract the text payload from a CallToolResult (or plain return)."""
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        first = content[0]
        return getattr(first, "text", first)
    return content


@pytest.fixture
def server(app_with_data):
    return create_mcp_server(app_with_data)


class TestMCPServer:
    def test_registers_ten_tools(self, server):
        tools = _await(server.list_tools())
        assert [t.name for t in tools] == list(TOOL_NAMES)
        assert all(t.description for t in tools)

    def test_call_tool_returns_json_text(self, server):
        result = _await(server.call_tool("list_sources", {}))
        payload = json.loads(_text(result))
        assert payload["sources"][0]["source"] == "test_db"

    def test_call_query_tool(self, server):
        result = _await(
            server.call_tool("query_data", {"sql": "SELECT COUNT(*) AS c FROM test_db.users"})
        )
        assert json.loads(_text(result))["rows"] == [{"c": 5}]

    def test_errors_are_json_not_exceptions(self, server):
        result = _await(server.call_tool("query_data", {"sql": "DROP TABLE test_db.users"}))
        assert "error" in json.loads(_text(result))

    def test_search_defaults(self, server):
        result = _await(
            server.call_tool(
                "search_data",
                {"table": "test_db.users", "filter_column": "name", "filter_value": "Alice"},
            )
        )
        assert json.loads(_text(result))["row_count"] == 1
