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


def test_deep_merge_nested_and_list_replace() -> None:
    """Nested dicts merge key-by-key; lists replace wholesale; inputs untouched."""
    from core.config import _deep_merge

    base = {"a": {"x": 1, "y": 2}, "list": [1, 2], "keep": 9}
    override = {"a": {"y": 20, "z": 30}, "list": [3]}
    assert _deep_merge(base, override) == {
        "a": {"x": 1, "y": 20, "z": 30},
        "list": [3],
        "keep": 9,
    }
    assert base["a"] == {"x": 1, "y": 2}  # not mutated


def test_settings_local_override_merges(tmp_path, monkeypatch) -> None:
    """A gitignored settings.local.yaml deep-merges over settings.yaml (manual_holdings)."""
    import core.config as config

    local = tmp_path / "settings.local.yaml"
    local.write_text(
        'sources:\n'
        '  binance_account:\n'
        '    manual_holdings:\n'
        '      - {label: "Test bot", value_usd: 123, note: local}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_LOCAL_PATH", local)
    load_settings.cache_clear()
    settings = load_settings()
    assert settings.source("binance_account").get("manual_holdings") == [
        {"label": "Test bot", "value_usd": 123, "note": "local"}
    ]
    assert "wallets" in settings.source("binance_account")  # base keys survive the merge
    load_settings.cache_clear()


def test_settings_local_absent_is_noop(tmp_path, monkeypatch) -> None:
    """No local file -> manual_holdings absent (the capital-moved-to-spot case)."""
    import core.config as config

    monkeypatch.setattr(config, "SETTINGS_LOCAL_PATH", tmp_path / "missing.yaml")
    load_settings.cache_clear()
    assert load_settings().source("binance_account").get("manual_holdings") is None
    load_settings.cache_clear()
