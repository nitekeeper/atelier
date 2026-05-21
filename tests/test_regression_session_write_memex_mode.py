# tests/test_regression_session_write_memex_mode.py
"""Regression for issue #6 bug #2 — backend_memex.upsert_session in memex mode.

Symptom on un-patched code:
    sqlite3.IntegrityError: NOT NULL constraint failed: sessions.workspace_id

Cause: backend_memex.upsert_session builds a payload without
`workspace_id`. The memex `sessions` table declares
`workspace_id INTEGER NOT NULL REFERENCES workspaces(id)`, so the INSERT
fails. In local mode, scripts/projects.py resolves and stores
workspace_id on the project row, and session.py's local path inherits
it; the memex path has no equivalent population step.

Fix shape: in backend_memex.upsert_session, look up the project's
workspace_id from the projects table before INSERT and inject it into
the payload.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from scripts import backend_memex


def test_session_write_in_memex_mode_populates_workspace_id():
    """upsert_session in memex mode must look up the project's workspace_id
    and include it in the INSERT payload so the sessions.workspace_id
    NOT NULL constraint is satisfied.

    On un-patched code, the insert payload omits workspace_id and the
    real memex stores.insert path raises sqlite3.IntegrityError. We
    simulate the constraint here so the test exercises the bug even
    against a mocked stores layer."""

    PROJECT_ID = 1
    PROJECT_WORKSPACE_ID = 7

    # Track what gets passed through to the eventual INSERT.
    captured: dict = {}

    def fake_query(*, store, table, where=None):
        # The lookup for existing in-progress sessions (the upsert
        # guard) must return empty so we take the INSERT branch.
        if table == "sessions":
            return []
        # The fix must look up the owning project row to fetch
        # workspace_id before insert.
        if table == "projects" and (where or {}).get("id") == PROJECT_ID:
            return [{"id": PROJECT_ID, "workspace_id": PROJECT_WORKSPACE_ID, "slug": "p"}]
        return []

    def fake_insert(*, store, table, row):
        captured["row"] = dict(row)
        # Emulate the real SQLite NOT NULL constraint so the bug is
        # caught even with the stores layer mocked.
        if table == "sessions" and "workspace_id" not in row:
            raise sqlite3.IntegrityError("NOT NULL constraint failed: sessions.workspace_id")
        return {"id": 11, **row}

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_core_insert", side_effect=fake_insert),
    ):
        try:
            session = backend_memex.upsert_session(
                project_id=PROJECT_ID,
                agent_id="dr-priya-nair",
                phase="tdd:red",
                current_tasks="X",
                accomplished="Y",
                next_action="Z",
                status="in-progress",
            )
        except sqlite3.IntegrityError as e:
            pytest.fail(
                f"upsert_session raised IntegrityError in memex mode (bug #2 not fixed): {e}"
            )

    # The INSERT row must have carried the project's workspace_id, and
    # the returned dict must reflect it for downstream callers.
    assert captured["row"]["workspace_id"] == PROJECT_WORKSPACE_ID
    assert captured["row"]["project_id"] == PROJECT_ID
    assert captured["row"]["agent_id"] == "dr-priya-nair"
    assert session["workspace_id"] == PROJECT_WORKSPACE_ID


def test_session_write_in_memex_mode_raises_value_error_on_unknown_project():
    """Operator-signal parity with backend_local._workspace_id_for_project:
    if the project_id doesn't exist, raise a clean ValueError rather
    than letting SQLite's IntegrityError obscure the root cause."""

    UNKNOWN_PROJECT_ID = 999

    def fake_query(*, store, table, where=None):
        if table == "sessions":
            return []
        return []  # no project row exists for UNKNOWN_PROJECT_ID

    with patch.object(backend_memex, "_memex_core_query", side_effect=fake_query):
        with pytest.raises(ValueError, match=f"project_id={UNKNOWN_PROJECT_ID}"):
            backend_memex.upsert_session(
                project_id=UNKNOWN_PROJECT_ID,
                agent_id="dr-priya-nair",
                phase="tdd:red",
            )
