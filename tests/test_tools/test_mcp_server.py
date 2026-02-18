"""Smoke tests for MCP server module."""

import pytest

from fusion.tools.mcp_server import main


class TestMCPServer:
    def test_main_function_exists(self):
        assert callable(main)

    def test_create_mcp_app_import(self):
        """Test that _create_mcp_app can be imported (mcp package optional)."""
        try:
            from fusion.tools.mcp_server import _create_mcp_app
            assert callable(_create_mcp_app)
        except ImportError:
            pytest.skip("mcp package not installed")
