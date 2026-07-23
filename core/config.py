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

    @property
    def assets(self) -> list[dict[str, Any]]:
        """Return the tracked-asset list (CLAUDE.md section 5)."""
        return list(self.raw.get("assets", []))

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

    db_rel = raw.get("database", {}).get("path", "cryptodash.db")
    db_path = Path(db_rel)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    log_level = os.getenv("LOG_LEVEL") or raw.get("logging", {}).get("level", "INFO")

    secrets = {
        key: os.environ[key]
        for key in ("FRED_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
        if os.environ.get(key)
    }

    return Settings(
        raw=raw,
        db_path=db_path,
        log_level=str(log_level).upper(),
        secrets=secrets,
    )
