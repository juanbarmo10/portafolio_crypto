"""Configuration loading for cryptodash.

Loads externalized settings from ``config/settings.yaml`` and secrets from
``config/.env`` (CLAUDE.md sections 10-11: nothing hardcoded, secrets never in
the repo). All filesystem paths are resolved relative to the repository root so
the project has no hardcoded absolute paths and stays reproducible across machines.

Inputs (read):
    - config/settings.yaml   : non-secret configuration.
    - config/.env (optional) : secrets and runtime overrides (via python-dotenv).
    - environment variables  : take precedence over .env, which takes precedence
                               over settings.yaml where they overlap.

Outputs:
    - Settings object exposing the parsed config plus resolved paths and secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Repository root = two levels up from this file (core/config.py -> repo root).
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
CONFIG_DIR: Path = REPO_ROOT / "config"
SETTINGS_PATH: Path = CONFIG_DIR / "settings.yaml"
ASSETS_META_PATH: Path = CONFIG_DIR / "assets_meta.yaml"
ENV_PATH: Path = CONFIG_DIR / ".env"


@dataclass(frozen=True)
class Settings:
    """Parsed configuration plus resolved paths and secrets.

    Attributes:
        raw: The full settings.yaml mapping, for sections without a typed accessor.
        db_path: Absolute path to the SQLite database file.
        log_level: Effective log level (LOG_LEVEL env var overrides settings.yaml).
        secrets: Selected secrets read from the environment/.env (may be empty).
    """

    raw: dict[str, Any]
    db_path: Path
    log_level: str
    secrets: dict[str, str] = field(default_factory=dict)
    asset_meta: dict[str, Any] = field(default_factory=dict)
    public_mode: bool = False  # hide the real account (level 4) for public deploys

    @property
    def assets(self) -> list[dict[str, Any]]:
        """Return the tracked-asset list (CLAUDE.md section 5)."""
        return list(self.raw.get("assets", []))

    def meta_for(self, symbol: str) -> dict[str, Any]:
        """Return display metadata (logo_url, description) for an asset symbol."""
        return dict(self.asset_meta.get(symbol, {}))

    def source(self, name: str) -> dict[str, Any]:
        """Return the parameter block for a named data source (e.g. 'coingecko')."""
        return dict(self.raw.get("sources", {}).get(name, {}))


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict, failing loudly if it is missing or malformed."""
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Settings file {path} did not parse to a mapping.")
    return data


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Load and cache the effective settings.

    Precedence for overlapping values: environment variable > .env file >
    settings.yaml. The result is cached; call :func:`load_settings.cache_clear`
    in tests that need to reload with a different environment.

    Returns:
        A frozen :class:`Settings` instance.
    """
    # load_dotenv does not override already-set environment variables, so real
    # env vars keep precedence over .env by construction.
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    raw = _load_yaml(SETTINGS_PATH)

    # Optional per-asset display metadata (logos, tooltips). Absent -> empty.
    asset_meta = _load_yaml(ASSETS_META_PATH) if ASSETS_META_PATH.exists() else {}

    db_rel = raw.get("database", {}).get("path", "cryptodash.db")
    db_path = Path(db_rel)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    log_level = os.getenv("LOG_LEVEL") or raw.get("logging", {}).get("level", "INFO")

    # Public/demo mode hides the real account section (level 4). Env var wins so a
    # deployed instance can set it without editing config.
    env_public = os.getenv("PUBLIC_MODE")
    if env_public is not None:
        public_mode = env_public.strip().lower() in {"1", "true", "yes", "on"}
    else:
        public_mode = bool(raw.get("app", {}).get("public_mode", False))

    secrets = {
        key: os.environ[key]
        for key in (
            "FRED_API_KEY",
            "TELEGRAM_TOKEN",
            "TELEGRAM_CHAT_ID",
            "BINANCE_API_KEY",     # READ-ONLY key (no trading/withdrawal), see .env.example
            "BINANCE_API_SECRET",
        )
        if os.environ.get(key)
    }

    return Settings(
        raw=raw,
        db_path=db_path,
        log_level=str(log_level).upper(),
        secrets=secrets,
        asset_meta=asset_meta,
        public_mode=public_mode,
    )
