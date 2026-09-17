"""Tests for the source registry."""

import pytest

from fusion.adapters.outbound.registry import SourceRegistry, default_registry
from fusion.adapters.outbound.warp.source import WarpSource
from fusion.domain.errors import ConnectionError
from tests.fakes import FakeDataSource, FakeWarpTransport


class TestSourceRegistry:
    def test_register_and_create(self):
        registry = SourceRegistry()
        registry.register("fake", lambda name, cfg: FakeDataSource(name, cfg.get("tables")))
        source = registry.create("s1", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        assert isinstance(source, FakeDataSource)
        assert source.name == "s1"
        assert registry.types() == ["fake"]

    def test_missing_type(self):
        with pytest.raises(ConnectionError, match="type"):
            SourceRegistry().create("s", {})

    def test_unknown_type(self):
        registry = SourceRegistry()
        registry.register("fake", lambda n, c: FakeDataSource(n))
        with pytest.raises(ConnectionError, match="Unknown source type 'nope'"):
            registry.create("s", {"type": "nope"})


class TestDefaultRegistry:
    def test_has_warp(self):
        assert default_registry().types() == ["warp"]

    def test_builds_warp_source_with_merged_defaults(self):
        transport = FakeWarpTransport({"t": []}, database="db")
        registry = default_registry({"timeout": 7, "page_size": 3})
        source = registry.create(
            "db", {"type": "warp", "base_url": "http://localhost:1", "transport": transport}
        )
        assert isinstance(source, WarpSource)
        assert source.database == "db"
        assert source._page_size == 3
        assert source._client.timeout == 7
