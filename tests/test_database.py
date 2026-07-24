"""Tests for the DB backend adapter (CLAUDE.md sections 8, 10).

The SQLite path is native and covered by the rest of the suite; here we test the pure
placeholder translation and that DATABASE_URL selection is honored.
"""

from __future__ import annotations

from pathlib import Path

from db.database import _PgConnection, _translate, database_url, open_connection
from db.loader import SCHEMA_PATH


class _FakeCursor:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def execute(self, sql: str, params: object = None) -> None:
        self._log.append(sql)


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)


def test_translate_qmark_to_psycopg() -> None:
    assert _translate("SELECT * FROM t WHERE a = ? AND b = ?", named=False) == (
        "SELECT * FROM t WHERE a = %s AND b = %s"
    )


def test_translate_named_to_psycopg() -> None:
    assert _translate("INSERT INTO t VALUES (:a, :b)", named=True) == (
        "INSERT INTO t VALUES (%(a)s, %(b)s)"
    )


def test_database_url_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() is None


def test_open_connection_uses_sqlite_without_env(monkeypatch, tmp_path: Path) -> None:
    import sqlite3

    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = open_connection(tmp_path / "x.db")
    try:
        assert isinstance(conn, sqlite3.Connection)  # native SQLite path unchanged
    finally:
        conn.close()


def test_pg_executescript_applies_schema_skipping_pragma() -> None:
    # The Postgres schema application must run every CREATE and skip SQLite-only PRAGMA
    # and comment lines — validated against the real schema.sql with a fake connection.
    fake = _FakeConn()
    _PgConnection(fake).executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    joined = " ".join(fake.executed)
    assert fake.executed  # something ran
    for table in ("observations", "events", "alerts_log", "dca_plan", "exit_rules", "trades"):
        assert table in joined
    assert "PRAGMA" not in joined.upper()  # SQLite-only, skipped
    # Full comment lines are stripped; inline "-- ..." after code stays (valid line
    # comment in Postgres). No statement should be *only* a comment.
    for stmt in fake.executed:
        assert not stmt.lstrip().startswith("--")
