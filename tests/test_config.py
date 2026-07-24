"""Tests for core.config: settings load and path resolution (CLAUDE.md section 10)."""

from __future__ import annotations

from core.config import REPO_ROOT, load_settings


def test_settings_load_and_db_path_absolute() -> None:
    """Settings load and db_path resolves to an absolute path under the repo root."""
    load_settings.cache_clear()
    settings = load_settings()
    assert settings.db_path.is_absolute()
    assert settings.db_path.parent == REPO_ROOT


def test_assets_present_and_shaped() -> None:
    """The asset universe loads with the required keys per asset (section 5)."""
    load_settings.cache_clear()
    settings = load_settings()
    assert len(settings.assets) >= 8
    for asset in settings.assets:
        assert "symbol" in asset
        assert "coingecko_id" in asset
        assert "tier" in asset


def test_log_level_env_override(monkeypatch) -> None:
    """LOG_LEVEL env var overrides settings.yaml logging.level."""
    monkeypatch.setenv("LOG_LEVEL", "debug")
    load_settings.cache_clear()
    settings = load_settings()
    assert settings.log_level == "DEBUG"
    load_settings.cache_clear()


def test_public_mode_defaults_false_and_env_override(monkeypatch) -> None:
    """public_mode is False by default; PUBLIC_MODE env var turns it on."""
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    load_settings.cache_clear()
    assert load_settings().public_mode is False

    monkeypatch.setenv("PUBLIC_MODE", "1")
    load_settings.cache_clear()
    assert load_settings().public_mode is True
    load_settings.cache_clear()
