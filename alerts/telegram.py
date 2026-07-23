"""Telegram alert delivery (CLAUDE.md sections 3, 8 phase 3).

A single ``TelegramSender`` with a ``send(msg)`` method (Markdown). If the bot token
or chat id are not configured it runs in **dry-run**: the message is logged, not sent,
so the whole alert pipeline is testable without a bot.

Setup (free): create a bot via @BotFather, put the token in ``TELEGRAM_TOKEN`` and your
chat id in ``TELEGRAM_CHAT_ID`` (see config/.env.example).
"""

from __future__ import annotations

import requests

from core.config import Settings
from core.logging_setup import get_logger

log = get_logger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramSender:
    """Sends Markdown messages to a Telegram chat, or logs them in dry-run."""

    def __init__(self, settings: Settings, timeout_s: int = 15) -> None:
        """Read the bot token / chat id from secrets. Missing -> dry-run."""
        self._token = settings.secrets.get("TELEGRAM_TOKEN")
        self._chat_id = settings.secrets.get("TELEGRAM_CHAT_ID")
        self._timeout = timeout_s

    @property
    def enabled(self) -> bool:
        """True when both token and chat id are configured (real sending)."""
        return bool(self._token and self._chat_id)

    def send(self, message: str) -> bool:
        """Send a Markdown message. Returns True if delivered, False in dry-run/error.

        In dry-run (unconfigured) the message is logged at INFO so pipelines still work.
        """
        if not self.enabled:
            log.info("Telegram dry-run (not configured):\n%s", message)
            return False
        try:
            resp = requests.post(
                _API.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException:
            log.exception("Telegram send failed.")
            return False
