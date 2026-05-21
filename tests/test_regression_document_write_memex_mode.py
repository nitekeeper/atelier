# tests/test_regression_document_write_memex_mode.py
"""Regression for issue #6 bug #3 — backend_memex.write_document in memex mode.

Symptom on un-patched code:
    sqlite3.IntegrityError: NOT NULL constraint failed:
        project_documents.workspace_id

Cause: backend_memex.write_document builds a payload that omits
`workspace_id`. The memex `project_documents` table declares
`workspace_id INTEGER NOT NULL REFERENCES workspaces(id)`, so the
INSERT fails on the first memex-mode design-doc write.

Note: the facade `backend.write_document` folds `workspace_id` into
`adapted_metadata` (backend.py:147) but the memex librarian's
`write_entry` does NOT promote metadata→payload. The fix must read
`workspace_id` from metadata (or fetch via project_id) and inject it
into the payload before `_atelier_write`.

Fix shape: in backend_memex.write_document, look up the project's
workspace_id (preferring metadata["workspace_id"] when the facade
populates it, else fetching from the projects row by project_id) and
inject it into the payload before `_atelier_write`. Mirrors the bug #2
pattern used in upsert_session.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from scripts import backend_memex


def test_write_document_in_memex_mode_populates_workspace_id():
    """write_document in memex mode must put workspace_id on the payload
    so the project_documents.workspace_id NOT NULL constraint is
    satisfied.

    On un-patched code the payload omits workspace_id and the real
    memex stores.insert path raises sqlite3.IntegrityError. We simulate
    the constraint here so the test exercises the bug even against a
    mocked stores layer."""

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
        if target_table == "project_documents" and "workspace_id" not in payload:
            raise sqlite3.IntegrityError(
                "NOT NULL constraint failed: project_documents.workspace_id"
            )
        return {"row_id": 99, "index_id": librarian_output["index_id"], **payload}

    # Bypass librarian validation / embedding / slug-resolution paths so
    # the test stays hermetic (no ~/.memex/config.json required).
    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_write_entry", side_effect=fake_write_entry),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="p"),
    ):
        try:
            backend_memex.write_document(
                domain="design",
                title="Design Doc",
                body="# Design\n\nbody",
                metadata={"project_id": PROJECT_ID, "workspace_id": PROJECT_WORKSPACE_ID},
                caller_agent_id="dr-samuel-okafor",
            )
        except sqlite3.IntegrityError as e:
            pytest.fail(
                f"write_document raised IntegrityError in memex mode (bug #3 not fixed): {e}"
            )

    assert captured["target_table"] == "project_documents"
    assert captured["payload"]["workspace_id"] == PROJECT_WORKSPACE_ID
    assert captured["payload"]["project_id"] == PROJECT_ID


def test_write_document_in_memex_mode_falls_back_to_project_lookup_when_metadata_missing():
    """If the caller / facade doesn't populate metadata['workspace_id'],
    the fix must fall back to looking up the owning project's
    workspace_id from the projects table. This protects against
    upstream callers who skip the facade adapter."""

    PROJECT_ID = 2
    PROJECT_WORKSPACE_ID = 7

    captured: dict = {}

    def fake_query(*, store, table, where=None):
        if table == "projects" and (where or {}).get("id") == PROJECT_ID:
            return [{"id": PROJECT_ID, "workspace_id": PROJECT_WORKSPACE_ID, "slug": "p"}]
        return []

    def fake_write_entry(
        *, payload, librarian_output, target_store, target_table, caller_agent_id, embedding=None
    ):
        captured["payload"] = dict(payload)
        if target_table == "project_documents" and "workspace_id" not in payload:
            raise sqlite3.IntegrityError(
                "NOT NULL constraint failed: project_documents.workspace_id"
            )
        return {"row_id": 99, "index_id": librarian_output["index_id"], **payload}

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_write_entry", side_effect=fake_write_entry),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="p"),
    ):
        backend_memex.write_document(
            domain="design",
            title="No-Metadata-WS Doc",
            body="body",
            # NOTE: metadata carries project_id but NOT workspace_id.
            metadata={"project_id": PROJECT_ID},
            caller_agent_id="dr-samuel-okafor",
        )

    assert captured["payload"]["workspace_id"] == PROJECT_WORKSPACE_ID


def test_write_document_in_memex_mode_raises_value_error_on_unknown_project():
    """Operator-signal parity with backend_local._workspace_id_for_project:
    if the project_id doesn't exist, raise a clean ValueError rather
    than letting SQLite's IntegrityError obscure the root cause."""

    UNKNOWN_PROJECT_ID = 999

    def fake_query(*, store, table, where=None):
        return []  # no projects, no workspaces — simulate unknown id

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_query),
        patch.object(backend_memex, "_memex_validate_output", side_effect=lambda x: x),
        patch.object(backend_memex, "_try_embed", return_value=None),
        patch.object(backend_memex, "_resolve_project_slug", return_value="p"),
    ):
        with pytest.raises(ValueError, match=f"project_id={UNKNOWN_PROJECT_ID}"):
            backend_memex.write_document(
                domain="design",
                title="X",
                body="b",
                metadata={"project_id": UNKNOWN_PROJECT_ID},
                caller_agent_id="dr-samuel-okafor",
            )
