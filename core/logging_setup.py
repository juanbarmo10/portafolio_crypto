"""Structured logging setup (CLAUDE.md section 10).

Provides a single ``configure_logging`` entry point and a ``get_logger`` helper.
Log records carry the ``source`` and ``series_id`` context fields when supplied
via ``extra=`` so ingest logs are filterable by data source (section 10).

The level is configurable at runtime through the LOG_LEVEL env var, resolved by
:func:`core.config.load_settings`.
"""

from __future__ import annotations

import logging
import sys

# Context fields we want on every record; default to '-' when not provided.
_CONTEXT_FIELDS = ("source", "series_id")

_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "src=%(source)s series=%(series_id)s | %(message)s"
)


class _ContextFilter(logging.Filter):
    """Guarantee context fields exist so the formatter never raises KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field_name in _CONTEXT_FIELDS:
            if not hasattr(record, field_name):
                setattr(record, field_name, "-")
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger idempotently.

    Args:
        level: Log level name (e.g. 'DEBUG', 'INFO'). Case-insensitive.

    Calling this more than once replaces existing handlers rather than stacking
    them, so repeated ingest runs do not duplicate log lines.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Assumes :func:`configure_logging` ran at startup."""
    return logging.getLogger(name)
