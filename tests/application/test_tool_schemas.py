"""Tests for tool schema definitions."""

from fusion.application.tool_schemas import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    get_mcp_tools,
    get_openai_tools,
)

EXPECTED = (
    "list_sources",
    "describe_table",
    "query_data",
    "search_data",
    "aggregate_data",
    "create_view",
    "list_views",
    "refresh_view",
    "load_table",
    "list_metrics",
    "query_metrics",
    "cache_stats",
)


class TestToolDefinitions:
    def test_tool_count_and_names(self):
        assert len(TOOL_DEFINITIONS) == 12
        assert TOOL_NAMES == EXPECTED

    def test_all_tools_have_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            assert set(tool) == {"name", "description", "parameters"}
            assert tool["parameters"]["type"] == "object"
            assert "properties" in tool["parameters"]
            assert "required" in tool["parameters"]

    def test_query_data_has_sql_param(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "query_data")
        assert "sql" in tool["parameters"]["properties"]
        assert "sql" in tool["parameters"]["required"]

    def test_aggregate_data_has_enum(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "aggregate_data")
        assert set(tool["parameters"]["properties"]["agg_func"]["enum"]) == {
            "SUM",
            "AVG",
            "COUNT",
            "MIN",
            "MAX",
        }


class TestFormats:
    def test_openai_tools_format(self):
        tools = get_openai_tools()
        assert len(tools) == 12
        for tool in tools:
            assert tool["type"] == "function"
            assert set(tool["function"]) == {"name", "description", "parameters"}

    def test_mcp_tools_format(self):
        tools = get_mcp_tools()
        assert len(tools) == 12
        for tool in tools:
            assert set(tool) == {"name", "description", "inputSchema"}
            assert tool["inputSchema"]["type"] == "object"

    def test_independent_copies(self):
        a, b = get_openai_tools(), get_openai_tools()
        a[0]["function"]["parameters"]["properties"]["x"] = 1
        assert "x" not in b[0]["function"]["parameters"]["properties"]
        m1, m2 = get_mcp_tools(), get_mcp_tools()
        m1[0]["inputSchema"]["properties"]["x"] = 1
        assert "x" not in m2[0]["inputSchema"]["properties"]
