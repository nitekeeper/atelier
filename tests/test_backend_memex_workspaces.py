"""Memex-mode workspace CRUD tests (atelier#51 / spec §10.1).

Mirrors `test_backend_local_workspaces.py` against the Memex-mode
implementation. `_memex_core_query` + `_memex_core_insert` are patched
via the shared mock_core fixture so tests don't need a real
~/.memex/atelier.db on disk.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import backend_memex


@pytest.fixture
def mock_core():
    """Patch Memex Core CRUD helpers — pure unit-test scope, no real
    Memex install required."""
    with (
        patch.object(backend_memex, "_memex_core_insert") as ins,
        patch.object(backend_memex, "_memex_core_query") as qry,
    ):
        yield {"insert": ins, "query": qry}


# ── find_workspace_by_identity ─────────────────────────────────────────────


def test_find_workspace_by_identity_returns_none_when_absent(mock_core):
    mock_core["query"].return_value = []
    assert backend_memex.find_workspace_by_identity(identity="repo:absent") is None
    mock_core["query"].assert_called_once_with(
        store="atelier", table="workspaces", where={"identity": "repo:absent"}
    )


def test_find_workspace_by_identity_returns_row_when_present(mock_core):
    mock_core["query"].return_value = [
        {
            "id": 7,
            "identity": "repo:auth",
            "slug": "auth",
            "name": "Auth",
            "description": None,
        }
    ]
    row = backend_memex.find_workspace_by_identity(identity="repo:auth")
    assert row is not None
    assert row["id"] == 7
    assert row["identity"] == "repo:auth"
    assert row["slug"] == "auth"


# ── find_or_create_workspace ───────────────────────────────────────────────


def test_find_or_create_workspace_returns_existing_without_insert(mock_core):
    """When `find_workspace_by_identity` hits, the function must NOT
    fire an insert (idempotency on identity)."""
    existing = {
        "id": 4,
        "identity": "repo:existing",
        "slug": "existing",
        "name": "Existing",
        "description": "",
    }
    mock_core["query"].return_value = [existing]
    row = backend_memex.find_or_create_workspace(
        identity="repo:existing",
        slug="changed-slug",  # different from existing — should NOT overwrite
        name="Changed",
        description="changed desc",
    )
    assert row == existing
    mock_core["insert"].assert_not_called()


def test_find_or_create_workspace_inserts_when_absent(mock_core):
    """When `find_workspace_by_identity` returns None, insert the row
    with the caller-supplied attributes + a now() timestamp."""
    mock_core["query"].return_value = []
    inserted = {
        "id": 11,
        "identity": "repo:new",
        "slug": "new",
        "name": "New",
        "description": "first create",
        "created_at": "2026-05-26T16:00:00Z",
        "updated_at": "2026-05-26T16:00:00Z",
    }
    mock_core["insert"].return_value = inserted
    row = backend_memex.find_or_create_workspace(
        identity="repo:new", slug="new", name="New", description="first create"
    )
    assert row == inserted
    # Insert was called once against the atelier store's workspaces table.
    mock_core["insert"].assert_called_once()
    kwargs = mock_core["insert"].call_args.kwargs
    assert kwargs["store"] == "atelier"
    assert kwargs["table"] == "workspaces"
    payload = kwargs["row"]
    assert payload["identity"] == "repo:new"
    assert payload["slug"] == "new"
    assert payload["name"] == "New"
    assert payload["description"] == "first create"
    # Timestamps are set by the helper, not the caller.
    assert "created_at" in payload
    assert "updated_at" in payload


def test_find_or_create_workspace_accepts_none_description(mock_core):
    """description=None (the default in the facade signature) flows
    through to the insert payload as None — not converted to empty
    string or dropped from the row."""
    mock_core["query"].return_value = []
    mock_core["insert"].return_value = {"id": 1}
    backend_memex.find_or_create_workspace(
        identity="repo:nullable", slug="nullable", name="Nullable"
    )
    payload = mock_core["insert"].call_args.kwargs["row"]
    assert payload["description"] is None


# ── list_workspaces ────────────────────────────────────────────────────────


def test_list_workspaces_empty_returns_empty(mock_core):
    mock_core["query"].return_value = []
    assert backend_memex.list_workspaces() == []
    mock_core["query"].assert_called_once_with(store="atelier", table="workspaces")


def test_list_workspaces_sorts_by_slug_ascending(mock_core):
    """Memex Core's `query` doesn't guarantee an ORDER BY; the facade
    sorts in Python so callers see a stable order regardless of
    underlying storage order."""
    mock_core["query"].return_value = [
        {"id": 3, "slug": "charlie"},
        {"id": 1, "slug": "alpha"},
        {"id": 2, "slug": "bravo"},
    ]
    rows = backend_memex.list_workspaces()
    assert [r["slug"] for r in rows] == ["alpha", "bravo", "charlie"]


def test_list_workspaces_handles_missing_slug_defensively(mock_core):
    """A row with a missing/None slug (shouldn't happen — NOT NULL — but
    defensive against future schema drift) sorts as if its slug is the
    empty string rather than raising TypeError."""
    mock_core["query"].return_value = [
        {"id": 2, "slug": "alpha"},
        {"id": 1, "slug": None},
    ]
    rows = backend_memex.list_workspaces()
    # None-slug row sorts first (treated as empty string).
    assert rows[0]["id"] == 1
    assert rows[1]["id"] == 2
