"""Smoke test: the Streamlit dashboard runs end to end without raising.

Uses Streamlit's AppTest to execute app/dashboard.py in-process and assert no
exception surfaced. This guards the rendering path (Styler colors, ImageColumn,
column_config) that unit tests over pure helpers cannot reach. Works whether or
not the database has data: with an empty DB the sections show info messages.
"""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from core.config import REPO_ROOT


def test_dashboard_runs_without_exception() -> None:
    app_path = str(REPO_ROOT / "app" / "dashboard.py")
    at = AppTest.from_file(app_path, default_timeout=60).run()
    assert not at.exception, at.exception
