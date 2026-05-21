# tests/test_regression_meeting_write_memex_mode.py
"""Regression for issue #6 bug #4 — backend_memex.write_meeting in memex mode.

Symptom on un-patched code:
    sqlite3.IntegrityError: NOT NULL constraint failed:
        meeting_minutes.workspace_id

Cause: backend_memex.write_meeting builds a payload that omits
`workspace_id`. The memex `meeting_minutes` table declares
`workspace_id INTEGER NOT NULL REFERENCES workspaces(id)`, so the
INSERT fails.

Special case: meeting_minutes.project_id is NULLABLE (workspace-level
meetings). When project_id is None the fix must fall back to a
single-workspace resolution against the memex atelier store
(matching `_resolve_workspace_id` in scripts/projects.py — we inline
the same logic here to avoid a circular dep on projects.py).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from scripts import backend_memex


def test_write_meeting_in_memex_mode_populates_workspace_id():
    """write_meeting with a project_id must look up the owning project's
    workspace_id and include it in the payload."""

    PROJECT_ID = 1
    PROJECT_WORKSPACE_ID = 42

    captured: dict = {}

    def fake_query(*, store, table, where=None):
        if table == "projects" and (where or {}).get("id") == PROJECT_ID:
            return [{"id": PROJECT_ID, "workspace_id": PROJECT_WORKSPACE_ID, "slug": "p"}]
        return []

    def fake_write_entry(
        *, payload, librarian_output, target_store, target_table, caller_agent_id, embedding=None
    ):
        captured["payload"] = dict(payload)
        captured["target_table"] = target_table
        if target_table == "meeting_minutes" and "workspace_id" not in payload:
            raise sqlite3.IntegrityError("NOT NULL constraint failed: meeting_minutes.workspace_id")
        return {"row_id": 11, "index_id": librarian_output["index_id"], **payload}

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_write_entry", side_effect=fake_write_entry),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="p"),
        patch.object(backend_memex, "_next_seq", return_value=1),
    ):
        try:
            backend_memex.write_meeting(
                title="Sprint Standup",
                date="2026-05-19",
                summary="Did stuff.",
                decisions="Decided things.",
                created_by="dr-samuel-okafor",
                project_id=PROJECT_ID,
            )
        except sqlite3.IntegrityError as e:
            pytest.fail(
                f"write_meeting raised IntegrityError in memex mode (bug #4 not fixed): {e}"
            )

    assert captured["target_table"] == "meeting_minutes"
    assert captured["payload"]["workspace_id"] == PROJECT_WORKSPACE_ID


def test_write_meeting_in_memex_mode_workspace_level_meeting_no_project_id():
    """A workspace-level meeting (project_id=None) must fall back to
    the single-workspace lookup against the memex atelier store. The
    payload must still carry a non-null workspace_id."""

    SINGLE_WS_ID = 1

    captured: dict = {}

    def fake_query(*, store, table, where=None):
        # No project lookup — project_id is None. The fix must query
        # workspaces directly.
        if table == "workspaces":
            return [{"id": SINGLE_WS_ID, "slug": "atelier", "name": "Atelier"}]
        return []

    def fake_write_entry(
        *, payload, librarian_output, target_store, target_table, caller_agent_id, embedding=None
    ):
        captured["payload"] = dict(payload)
        if target_table == "meeting_minutes" and "workspace_id" not in payload:
            raise sqlite3.IntegrityError("NOT NULL constraint failed: meeting_minutes.workspace_id")
        return {"row_id": 12, "index_id": librarian_output["index_id"], **payload}

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_write_entry", side_effect=fake_write_entry),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="(no-project)"),
        patch.object(backend_memex, "_next_seq", return_value=1),
    ):
        backend_memex.write_meeting(
            title="All-Hands",
            date="2026-05-19",
            summary="Workspace-level.",
            decisions="None.",
            created_by="dr-samuel-okafor",
            project_id=None,
        )

    assert captured["payload"]["workspace_id"] == SINGLE_WS_ID


def test_write_meeting_in_memex_mode_raises_value_error_on_unknown_project():
    """When a project_id is supplied but doesn't exist, raise a clean
    ValueError rather than letting SQLite's IntegrityError obscure
    the root cause."""

    UNKNOWN_PROJECT_ID = 999

    def fake_query(*, store, table, where=None):
        return []  # neither the project nor any workspaces

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="p"),
    ):
        with pytest.raises(ValueError, match=f"project_id={UNKNOWN_PROJECT_ID}"):
            backend_memex.write_meeting(
                title="X",
                date="2026-05-19",
                summary="",
                decisions="",
                created_by="dr-samuel-okafor",
                project_id=UNKNOWN_PROJECT_ID,
            )
