"""Tests for Settings."""

from fusion.application.settings import Settings


class TestFromEnv:
    def test_defaults_when_env_empty(self):
        s = Settings.from_env({})
        assert s.env == "development"
        assert s.port == 9000
        assert s.warp_url == "http://localhost:8000"
        assert s.external_access is False
        assert s.cors_origins == ("http://localhost:3000",)
        assert s.log_level == "INFO"
        assert s.backup_enabled is False

    def test_parses_types(self):
        s = Settings.from_env(
            {
                "FUSION_ENV": "production",
                "FUSION_PORT": "8080",
                "FUSION_THREADS": "8",
                "FUSION_DUCKDB_EXTERNAL_ACCESS": "TRUE",
                "FUSION_MAX_INGEST_ROWS": "1000",
                "FUSION_CORS_ORIGINS": "https://a.com, https://b.com",
                "FUSION_LOG_LEVEL": "debug",
                "WARP_TIMEOUT": "12.5",
                "FUSION_BACKUP_ENABLED": "true",
                "FUSION_API_KEY": "k",
            }
        )
        assert s.is_production()
        assert s.port == 8080
        assert s.threads == 8
        assert s.external_access is True
        assert s.max_ingest_rows == 1000
        assert s.cors_origins == ("https://a.com", "https://b.com")
        assert s.log_level == "DEBUG"
        assert s.warp_timeout == 12.5
        assert s.backup_enabled is True
        assert s.requires_auth()

    def test_does_not_read_environment_at_import(self, monkeypatch):
        monkeypatch.setenv("FUSION_PORT", "1234")
        assert Settings().port == 9000
        assert Settings.from_env().port == 1234


class TestValidate:
    def test_production_placeholder_key_fails(self):
        errors = Settings(
            env="production", api_key="your-secure-api-key-here-change-in-production"
        ).validate()
        assert any("API_KEY" in e for e in errors)

    def test_production_empty_key_fails(self):
        assert Settings(env="production", api_key="").validate()

    def test_production_real_key_ok(self):
        s = Settings(env="production", api_key="s3cr3t", cors_origins=("https://app.example.com",))
        assert s.validate() == []

    def test_production_wildcard_cors_fails(self):
        errors = Settings(env="production", api_key="s3cr3t", cors_origins=("*",)).validate()
        assert any("CORS" in e for e in errors)

    def test_development_empty_key_ok(self):
        assert Settings(env="development", api_key="").validate() == []


class TestDebugEndpoints:
    def test_disabled_in_production_by_default(self):
        assert Settings(env="production").debug_endpoints_enabled() is False

    def test_enabled_outside_production_by_default(self):
        assert Settings(env="development").debug_endpoints_enabled() is True

    def test_force_enable_in_production(self):
        assert Settings(env="production", debug_endpoints="true").debug_endpoints_enabled() is True

    def test_force_disable_outside_production(self):
        assert (
            Settings(env="development", debug_endpoints="false").debug_endpoints_enabled() is False
        )


def test_warp_http_defaults():
    s = Settings(warp_timeout=5, warp_max_retries=1, pool_size=2, circuit_breaker_threshold=9)
    d = s.warp_http_defaults()
    assert d["timeout"] == 5
    assert d["max_retries"] == 1
    assert d["pool_size"] == 2
    assert d["circuit_breaker_threshold"] == 9
