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
    def test_registers_twelve_tools(self, server):
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


def test_load_table_accepts_a_slice(server):
    result = _await(
        server.call_tool(
            "load_table",
            {"table": "test_db.orders", "where": "product = 'A'", "columns": ["id", "product"]},
        )
    )
    payload = json.loads(_text(result))
    assert payload["status"] == "loaded"
    assert payload["slice"] == "columns=id,product where product = 'A'"
    assert payload["row_count"] == 3


def test_tool_descriptions_are_the_shared_ones(server):
    from fusion.application.tool_schemas import TOOL_DEFINITIONS

    expected = {d["name"]: d["description"] for d in TOOL_DEFINITIONS}
    assert {t.name: t.description for t in _await(server.list_tools())} == expected


def test_input_schemas_are_the_shared_ones(server):
    """Parameter descriptions and the filter shape reach an MCP client too."""
    from fusion.application.tool_schemas import TOOL_DEFINITIONS

    expected = {d["name"]: d["parameters"] for d in TOOL_DEFINITIONS}
    published = {t.name: t.input_schema for t in _await(server.list_tools())}
    assert published == expected
    filters = published["query_metrics"]["properties"]["filters"]
    assert "gte" in filters["items"]["properties"]["op"]["enum"]
    assert published["query_metrics"]["properties"]["order_by"]["description"]


def test_query_metrics_takes_the_published_filter_shape(server):
    result = _await(
        server.call_tool(
            "query_metrics",
            {
                "table": "test_db.orders",
                "metrics": ["amount:sum"],
                "dimensions": ["product"],
                "filters": [{"column": "amount", "op": "gte", "value": 75}],
                "order_by": "-amount_sum",
            },
        )
    )
    payload = json.loads(_text(result))
    assert [(r["product"], r["amount_sum"]) for r in payload["rows"]] == [
        ("A", 300.0),
        ("C", 150.0),
        ("B", 75.0),
    ]


def test_a_tool_that_publishes_other_parameters_is_refused(app_with_data, monkeypatch):
    from copy import deepcopy

    from fusion.adapters.inbound.mcp import server as module

    drifted = deepcopy(module.TOOL_DEFINITIONS)
    next(d for d in drifted if d["name"] == "describe_table")["parameters"]["properties"][
        "schema"
    ] = {"type": "string"}
    monkeypatch.setattr(module, "TOOL_DEFINITIONS", drifted)
    with pytest.raises(TypeError, match="describe_table"):
        create_mcp_server(app_with_data)
