# tests/test_regression_projects_create_memex_mode.py
"""Regression for issue #6 bug #1 — projects.create_project in memex mode.

Symptom on un-patched code:
    sqlite3.OperationalError: no such table: workspaces

Cause: `scripts.projects._resolve_workspace_id` unconditionally opens
`backend_local._conn()` and selects from the local `workspaces` table,
even when the active backend is memex. The local atelier.db is not
provisioned in memex mode, so the query crashes.

Fix shape: gate the local resolution on mode_detector.detect_mode()
== "local"; in memex mode, resolve workspace_id from the memex atelier
store (singleton workspace via _singleton_workspace — post atelier#55
the `_WORKSPACE_SLUG` hardcoding is gone).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from scripts import backend, backend_memex, mode_detector, projects


@pytest.fixture
def memex_mode(monkeypatch):
    """Pin mode_detector to 'memex' for the duration of the test."""
    mode_detector._clear_cache()
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    yield
    mode_detector._clear_cache()


def test_create_project_in_memex_mode_does_not_query_local_workspaces(memex_mode):
    """create_project() in memex mode must NOT touch the local workspaces
    table. It must resolve workspace_id via the memex backend and hand
    it to backend.write_project, then return a project dict with an id.

    On un-patched code this raises sqlite3.OperationalError: no such
    table: workspaces, because _resolve_workspace_id is called
    unconditionally and opens backend_local._conn().
    """

    # Mock the memex atelier store: a single workspace exists with id=1.
    def fake_core_query(*, store, table, where=None):
        if store == "atelier" and table == "workspaces":
            return [{"id": 1, "slug": "atelier", "name": "Atelier"}]
        return []

    # Mock write_project so we don't touch the real memex librarian; we
    # only care that workspace_id=1 was resolved and passed through.
    written: dict = {}

    def fake_write_project(*, workspace_id, slug, name, description, created_by):
        written.update(
            {
                "workspace_id": workspace_id,
                "slug": slug,
                "name": name,
                "description": description,
                "created_by": created_by,
            }
        )
        return {
            "row_id": 42,
            "workspace_id": workspace_id,
            "slug": slug,
            "name": name,
            "description": description,
            "phase": "design:open",
            "created_by": created_by,
        }

    with (
        patch.object(backend_memex, "_memex_core_query", side_effect=fake_core_query),
        patch.object(backend, "write_project", side_effect=fake_write_project),
    ):
        # Must NOT raise sqlite3.OperationalError: no such table: workspaces.
        try:
            row = projects.create_project(
                db_path=".ai/atelier.db",
                name="Reg Bug 1",
                description="memex-mode regression",
                created_by="someone-1",
            )
        except sqlite3.OperationalError as e:
            pytest.fail(
                f"create_project raised OperationalError in memex mode (bug #1 not fixed): {e}"
            )

    # Sanity: the fix must produce a usable project dict with an id and the
    # write_project facade must have been called with a real workspace_id.
    assert row["id"] == 42
    assert row["name"] == "Reg Bug 1"
    assert written["workspace_id"] == 1
    assert written["slug"] == "reg-bug-1"


def test_write_task_in_memex_mode_folds_team_pk_into_metadata(memex_mode):
    """atelier#90 part 2 — `backend.write_task(..., team_pk='run-A')` in
    Memex mode must NOT throw.

    `backend_memex.write_task` has a NARROWER signature with NO `team_pk`
    column (tasks live in the Memex Index, which has no such column), so the
    facade folds `team_pk` into `adapted_metadata` (exactly like `subdomain`)
    rather than passing it as an unknown kwarg. Passing it through would raise
    `TypeError: write_task() got an unexpected keyword argument 'team_pk'`.

    status's per-cycle FILTER is Local-only, but the WRITE path must survive
    Memex mode — this binds that.
    """
    captured: dict = {}

    def fake_memex_write_task(*, metadata=None, **kwargs):
        # Capture the metadata blob the facade folded team_pk into; echo a
        # minimal write result so create_task / callers see a row shape.
        captured["metadata"] = dict(metadata) if metadata else None
        captured["kwargs"] = kwargs
        return {"row_id": 7, "index_id": "idx-7"}

    with patch.object(backend_memex, "write_task", side_effect=fake_memex_write_task):
        # Must NOT raise (no unexpected-kwarg TypeError); team_pk is folded.
        result = backend.write_task(
            workspace_id=1,
            project_id=1,
            title="cycle-tagged",
            description="d",
            subdomain="bug",
            created_by="atelier-pm-1",
            team_pk="run-A",
        )

    assert result["row_id"] == 7
    # team_pk was folded into the metadata blob, NOT passed as a raw kwarg.
    assert "team_pk" not in captured["kwargs"], (
        "team_pk must NOT reach backend_memex.write_task as a kwarg (it has no such parameter)"
    )
    assert captured["metadata"] is not None
    assert captured["metadata"]["team_pk"] == "run-A"
    # subdomain is folded alongside it (the established adapter pattern).
    assert captured["metadata"]["subdomain"] == "bug"


def test_write_task_in_memex_mode_omits_team_pk_when_none(memex_mode):
    """Back-compat: when `team_pk` is None the facade must NOT plant a
    `team_pk: None` key in the Memex metadata blob (mirrors the `subdomain
    is not None` fold guard) — so a no-team_pk write looks exactly as before."""
    captured: dict = {}

    def fake_memex_write_task(*, metadata=None, **kwargs):
        captured["metadata"] = dict(metadata) if metadata else None
        return {"row_id": 8, "index_id": "idx-8"}

    with patch.object(backend_memex, "write_task", side_effect=fake_memex_write_task):
        backend.write_task(
            workspace_id=1,
            project_id=1,
            title="untagged",
            description="d",
            subdomain=None,
            created_by="atelier-pm-1",
        )

    # No subdomain + no team_pk → no adapted_metadata at all (None passed).
    assert captured["metadata"] is None
