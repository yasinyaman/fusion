"""Tests for tool definitions."""

from fusion.tools.definitions import (
    TOOL_DEFINITIONS,
    get_mcp_tools,
    get_openai_tools,
)


class TestToolDefinitions:
    def test_tool_count(self):
        assert len(TOOL_DEFINITIONS) == 10

    def test_all_tools_have_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool missing 'description': {tool}"
            assert "parameters" in tool, f"Tool missing 'parameters': {tool}"
            assert tool["parameters"]["type"] == "object"
            assert "properties" in tool["parameters"]
            assert "required" in tool["parameters"]

    def test_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        expected = [
            "list_sources", "describe_table", "query_data", "search_data",
            "aggregate_data", "create_view", "list_views", "refresh_view",
            "load_table", "cache_stats",
        ]
        assert names == expected

    def test_query_data_has_sql_param(self):
        query_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "query_data")
        assert "sql" in query_tool["parameters"]["properties"]
        assert "sql" in query_tool["parameters"]["required"]

    def test_describe_table_has_table_param(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "describe_table")
        assert "table" in tool["parameters"]["properties"]
        assert "table" in tool["parameters"]["required"]

    def test_aggregate_data_has_enum(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "aggregate_data")
        agg_func = tool["parameters"]["properties"]["agg_func"]
        assert "enum" in agg_func
        assert "SUM" in agg_func["enum"]
        assert "AVG" in agg_func["enum"]


class TestOpenAIFormat:
    def test_openai_tools_format(self):
        tools = get_openai_tools()
        assert len(tools) == 10

        for tool in tools:
            assert tool["type"] == "function"
            assert "function" in tool
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

    def test_openai_tools_are_independent_copies(self):
        tools1 = get_openai_tools()
        tools2 = get_openai_tools()
        # Mutating one shouldn't affect the other
        tools1[0]["function"]["name"] = "modified"
        assert tools2[0]["function"]["name"] != "modified"


class TestMCPFormat:
    def test_mcp_tools_format(self):
        tools = get_mcp_tools()
        assert len(tools) == 10

        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_mcp_tools_are_independent_copies(self):
        tools1 = get_mcp_tools()
        tools2 = get_mcp_tools()
        tools1[0]["name"] = "modified"
        assert tools2[0]["name"] != "modified"
