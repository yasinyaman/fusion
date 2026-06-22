"""Tests for configuration validation (production fail-fast)."""

from fusion.config import Config


class TestConfigValidation:
    def test_production_placeholder_key_fails(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(
            Config, "API_KEY", "your-secure-api-key-here-change-in-production"
        )
        errors = Config.validate()
        assert any("API_KEY" in e for e in errors)

    def test_production_empty_key_fails(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "API_KEY", "")
        assert Config.validate()

    def test_production_real_key_ok(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "API_KEY", "s3cr3t-strong-unique-key")
        monkeypatch.setattr(Config, "CORS_ORIGINS", ["https://app.example.com"])
        assert Config.validate() == []

    def test_production_wildcard_cors_fails(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "API_KEY", "s3cr3t-strong-unique-key")
        monkeypatch.setattr(Config, "CORS_ORIGINS", ["*"])
        errors = Config.validate()
        assert any("CORS" in e for e in errors)

    def test_development_empty_key_ok(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "development")
        monkeypatch.setattr(Config, "API_KEY", "")
        assert Config.validate() == []


class TestDebugEndpointsConfig:
    def test_disabled_in_production_by_default(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "")
        assert Config.debug_endpoints_enabled() is False

    def test_enabled_outside_production_by_default(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "development")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "")
        assert Config.debug_endpoints_enabled() is True

    def test_force_enable_in_production(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "production")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "true")
        assert Config.debug_endpoints_enabled() is True

    def test_force_disable_outside_production(self, monkeypatch):
        monkeypatch.setattr(Config, "ENV", "development")
        monkeypatch.setattr(Config, "DEBUG_ENDPOINTS", "false")
        assert Config.debug_endpoints_enabled() is False
