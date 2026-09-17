"""Tests for the CLI entry points."""

import pytest

from fusion.adapters.inbound.cli import mcp_main, rest_main
from fusion.adapters.inbound.cli.common import connect_sources, parse_source_spec
from fusion.domain.errors import ConnectionError
from tests.fakes import FakeSourceFactory, ManualScheduler


class TestParseSourceSpec:
    def test_full_spec(self):
        assert parse_source_spec("name=a,url=http://x:1,db=z") == {
            "name": "a",
            "url": "http://x:1",
            "db": "z",
        }

    def test_db_defaults_to_name(self):
        assert parse_source_spec("name=a,url=http://x")["db"] == "a"

    @pytest.mark.parametrize("spec", ["name=a", "url=http://x", "garbage", ""])
    def test_invalid(self, spec):
        with pytest.raises(ValueError):
            parse_source_spec(spec)


class _Discovery:
    def __init__(self, databases):
        self.databases = databases
        self.calls = []

    def discover_databases(self, base_url, api_key=None, timeout=30.0):
        self.calls.append(base_url)
        return self.databases


class TestConnectSources:
    def test_single_database(self, app, factory):
        names = connect_sources(app, "http://w:1", "primary_db")
        assert names == ["primary_db"]
        assert factory.sources["primary_db"].connected

    def test_auto_discover(self, app, factory):
        discovery = _Discovery(["a", "b"])
        names = connect_sources(
            app, "http://w:1", "ignored", auto_discover=True, discovery=discovery
        )
        assert names == ["a", "b"]
        assert discovery.calls == ["http://w:1"]
        assert set(factory.sources) == {"a", "b"}

    def test_auto_discover_falls_back_to_database(self, app, factory):
        names = connect_sources(app, "http://w:1", "fallback", True, discovery=_Discovery([]))
        assert names == ["fallback"]

    def test_extra_sources(self, app, factory):
        names = connect_sources(app, "http://w:1", "main", extra=["name=x,url=http://y:2,db=z"])
        assert names == ["main", "x"]
        assert "x" in factory.sources


class TestRestMain:
    def test_config_validation_fails_fast(self):
        code = rest_main.main([], environ={"FUSION_ENV": "production", "FUSION_API_KEY": ""})
        assert code == 1

    def test_connect_failure_returns_1(self, monkeypatch, tmp_path):
        from fusion.bootstrap import build_app

        factory = FakeSourceFactory()

        def fake_build(settings):
            return build_app(settings, scheduler=ManualScheduler(), source_factory=factory)

        monkeypatch.setattr(rest_main, "build_app", fake_build)

        def failing_connect(*args, **kwargs):
            raise ConnectionError("no warp")

        monkeypatch.setattr(rest_main, "connect_sources", failing_connect)
        code = rest_main.main(
            ["--warp-url", "http://w:1"],
            environ={"FUSION_LOG_FILE": "", "FUSION_LOG_FORMAT": "text"},
        )
        assert code == 1

    def test_serves_with_uvicorn(self, monkeypatch, tmp_path):
        import uvicorn

        from fusion.bootstrap import build_app

        factory = FakeSourceFactory()
        built = {}

        def fake_build(settings):
            built["app"] = build_app(settings, scheduler=ManualScheduler(), source_factory=factory)
            return built["app"]

        runs = []
        monkeypatch.setattr(rest_main, "build_app", fake_build)
        monkeypatch.setattr(
            uvicorn.Server, "run", lambda self, sockets=None: runs.append(self.config)
        )
        code = rest_main.main(
            ["--warp-url", "http://w:1", "--database", "db", "--port", "9999"],
            environ={"FUSION_LOG_FILE": "", "FUSION_LOG_FORMAT": "text"},
        )
        assert code == 0
        assert runs and runs[0].port == 9999
        assert "db" in factory.sources
        assert factory.sources["db"].closed  # fusion.close() ran in finally

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            rest_main.main(["--version"], environ={})
        assert exc.value.code == 0
        assert "fusion" in capsys.readouterr().out


class TestMcpMain:
    def test_runs_stdio_server(self, monkeypatch):
        from fusion.bootstrap import build_app

        factory = FakeSourceFactory()
        transports = []

        def fake_build(settings):
            assert settings.memory_limit == "300MB"
            assert settings.threads == 1
            return build_app(settings, scheduler=ManualScheduler(), source_factory=factory)

        class FakeServer:
            def run(self, transport="stdio", **kwargs):
                transports.append(transport)

        monkeypatch.setattr(mcp_main, "build_app", fake_build)
        monkeypatch.setattr(
            "fusion.adapters.inbound.mcp.server.create_mcp_server", lambda fusion: FakeServer()
        )
        code = mcp_main.main(
            [
                "--warp-url",
                "http://w:1",
                "--database",
                "db",
                "--memory-limit",
                "300MB",
                "--threads",
                "1",
            ],
            environ={},
        )
        assert code == 0
        assert transports == ["stdio"]
        assert factory.sources["db"].closed

    def test_connect_failure_returns_1(self, monkeypatch):
        from fusion.bootstrap import build_app

        monkeypatch.setattr(
            mcp_main,
            "build_app",
            lambda settings: build_app(
                settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory()
            ),
        )
        monkeypatch.setattr(
            mcp_main, "connect_sources", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x"))
        )
        assert mcp_main.main(["--warp-url", "http://w:1"], environ={}) == 1
