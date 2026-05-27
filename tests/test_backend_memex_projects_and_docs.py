"""Memex-mode project + document CRUD tests (atelier#52 / spec §10.1).

Mirrors `test_backend_local_projects_and_docs.py` against the Memex-mode
implementation. `_memex_core_query` is patched via the shared mock_core
fixture so tests don't need a real ~/.memex/atelier.db.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import backend_memex


@pytest.fixture
def mock_core():
    """Patch Memex Core query helper — pure unit-test scope, no real
    Memex install required."""
    with patch.object(backend_memex, "_memex_core_query") as qry:
        yield {"query": qry}


# ── find_project ───────────────────────────────────────────────────────────


def test_find_project_returns_none_when_absent(mock_core):
    mock_core["query"].return_value = []
    assert backend_memex.find_project(workspace_id=5, slug="missing") is None
    mock_core["query"].assert_called_once_with(
        store="atelier",
        table="projects",
        where={"workspace_id": 5, "slug": "missing"},
    )


def test_find_project_returns_row_when_present(mock_core):
    row = {"id": 3, "workspace_id": 5, "slug": "auth", "name": "Auth"}
    mock_core["query"].return_value = [row]
    assert backend_memex.find_project(workspace_id=5, slug="auth") == row


def test_find_project_composite_key_workspace_scoped(mock_core):
    """The query MUST filter by BOTH workspace_id and slug — not just
    slug. Same-slug projects in different workspaces are distinct
    rows per §10.1."""
    mock_core["query"].return_value = []
    backend_memex.find_project(workspace_id=99, slug="shared")
    where = mock_core["query"].call_args.kwargs["where"]
    assert where == {"workspace_id": 99, "slug": "shared"}


# ── list_projects ──────────────────────────────────────────────────────────


def test_list_projects_empty_returns_empty(mock_core):
    mock_core["query"].return_value = []
    assert backend_memex.list_projects(workspace_id=1) == []
    mock_core["query"].assert_called_once_with(
        store="atelier", table="projects", where={"workspace_id": 1}
    )


def test_list_projects_sorts_by_slug_ascending(mock_core):
    """Memex Core's query doesn't guarantee ORDER BY; Python-side sort
    matches the backend_local contract."""
    mock_core["query"].return_value = [
        {"id": 3, "workspace_id": 1, "slug": "charlie"},
        {"id": 1, "workspace_id": 1, "slug": "alpha"},
        {"id": 2, "workspace_id": 1, "slug": "bravo"},
    ]
    rows = backend_memex.list_projects(workspace_id=1)
    assert [r["slug"] for r in rows] == ["alpha", "bravo", "charlie"]


def test_list_projects_handles_missing_slug_defensively(mock_core):
    """A row with None slug (shouldn't happen — NOT NULL — but defensive
    against future schema drift) sorts as if its slug were ''."""
    mock_core["query"].return_value = [
        {"id": 2, "slug": "alpha"},
        {"id": 1, "slug": None},
    ]
    rows = backend_memex.list_projects(workspace_id=1)
    assert rows[0]["id"] == 1  # None-slug first
    assert rows[1]["id"] == 2


# ── get_document ───────────────────────────────────────────────────────────


def test_get_document_returns_none_when_absent(mock_core):
    mock_core["query"].return_value = []
    assert backend_memex.get_document(doc_id=42) is None
    mock_core["query"].assert_called_once_with(
        store="atelier", table="project_documents", where={"id": 42}
    )


def test_get_document_returns_row_when_present(mock_core):
    row = {
        "id": 42,
        "workspace_id": 1,
        "project_id": 7,
        "domain": "design",
        "subdomain": "auth",
        "title": "Auth Design",
        "filename": "docs/auth-design.md",
        "index_id": "abc-uuid",
    }
    mock_core["query"].return_value = [row]
    assert backend_memex.get_document(doc_id=42) == row


def test_get_document_does_not_query_memex_index_for_uuid(mock_core):
    """`doc_id` is the integer `project_documents.id` — NOT the Memex
    Index `index_id` UUID string. The query must target the atelier
    store's `project_documents` table, not the `index` store."""
    mock_core["query"].return_value = []
    backend_memex.get_document(doc_id=1)
    kwargs = mock_core["query"].call_args.kwargs
    assert kwargs["store"] == "atelier"
    assert kwargs["table"] == "project_documents"
    assert kwargs["where"] == {"id": 1}
