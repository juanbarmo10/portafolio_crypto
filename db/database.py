"""Database backend selection: SQLite (local) or PostgreSQL (cloud) — CLAUDE.md §3, §8 phase 4.

The whole codebase talks to a connection with the sqlite3 interface it already uses
(``execute``/``executemany``/``executescript``/``commit``/``close`` and ``with conn:``).
This module returns either:

- a **native sqlite3 connection** (default; the SQLite path is completely unchanged), or
- a thin **Postgres wrapper** that mimics that same interface, when ``DATABASE_URL`` is set.

Only the Postgres wrapper translates the SQL (``?`` -> ``%s``, ``:name`` -> ``%(name)s``)
and applies the schema statement-by-statement (skipping SQLite-only ``PRAGMA``). This keeps
the long-format schema and the ``ON CONFLICT`` upserts portable across both backends.

Postgres needs the optional extra: ``pip install -e ".[postgres]"`` (psycopg).
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from core.logging_setup import get_logger

log = get_logger(__name__)

_NAMED = re.compile(r":(\w+)")


def _translate(sql: str, named: bool) -> str:
    """Translate sqlite-style placeholders to psycopg style for Postgres."""
    return _NAMED.sub(r"%(\1)s", sql) if named else sql.replace("?", "%s")


class _PgCursor:
    """Wrap a psycopg cursor so row access matches sqlite3 (tuple rows)."""

    def __init__(self, cursor: Any) -> None:
        self._cur = cursor

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def __iter__(self):
        # sqlite3 cursors are iterable (row-by-row); mirror that for parity so
        # `for row in conn.execute(...)` works on both backends.
        return iter(self._cur)


class _PgConnection:
    """Minimal sqlite3-like facade over a psycopg connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] | dict | None = None) -> _PgCursor:
        cur = self._conn.cursor()
        named = isinstance(params, dict)
        cur.execute(_translate(sql, named), params if params is not None else None)
        return _PgCursor(cur)

    def executemany(self, sql: str, seq: Sequence[Any]) -> None:
        seq = list(seq)
        named = bool(seq) and isinstance(seq[0], dict)
        cur = self._conn.cursor()
        cur.executemany(_translate(sql, named), seq)

    def executescript(self, script: str) -> None:
        # Postgres has no executescript: run each statement. Strip line comments (-- to
        # end of line) FIRST — a ';' inside a comment (e.g. "ledger; used to...") would
        # otherwise split a statement in two. Then skip SQLite-only PRAGMA. (The schema
        # has no '--' inside string literals, so this comment removal is safe.)
        no_comments = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
        for raw in no_comments.split(";"):
            code = raw.strip()
            if not code or code.upper().startswith("PRAGMA"):
                continue
            self._conn.cursor().execute(code)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "_PgConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()


def database_url() -> str | None:
    """Return DATABASE_URL if set (Postgres), else None (use SQLite)."""
    url = os.getenv("DATABASE_URL")
    return url.strip() if url else None


def open_connection(sqlite_path: Path) -> sqlite3.Connection | _PgConnection:
    """Open the backend connection: Postgres if DATABASE_URL is set, else SQLite.

    Args:
        sqlite_path: Path used when falling back to SQLite (parent dirs are created).
    """
    url = database_url()
    if url:
        try:
            import psycopg  # noqa: PLC0415 — optional dependency, only for Postgres
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "DATABASE_URL is set but psycopg is not installed. "
                'Install the extra: pip install -e ".[postgres]".'
            ) from exc
        log.info("Using PostgreSQL backend (DATABASE_URL).")
        return _PgConnection(psycopg.connect(url))

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
