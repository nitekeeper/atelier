"""Local-mode workspace CRUD tests (atelier#51 / spec §10.1).

Covers the three stubs lifted from `_not_implemented` to real
implementations:

- `find_or_create_workspace` — idempotent on `identity`, race-safe via
  INSERT OR IGNORE + SELECT
- `find_workspace_by_identity` — lookup by the §10.1 canonical key
- `list_workspaces` — ordered by slug

Local-mode tests; the parallel Memex-mode tests live in
`test_backend_memex_state.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Stand up a fake workspace root with .ai/atelier.db migrated.

    No seed rows — the tests here are the FIRST writers into the
    workspaces table, so the fixture only handles the schema apply.

    Forces Local mode via `mode_detector.detect_mode` patch — without
    this, the resolve_scope integration test would dispatch to Memex
    on dev machines where ~/.memex is installed. Same hardening
    pattern as test_tasks.py's setup fixture.
    """
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    return {"root": root, "db": str(db)}


# ── find_workspace_by_identity ─────────────────────────────────────────────


def test_find_workspace_by_identity_returns_none_for_unknown(workspace_root):
    """Identity that doesn't exist returns None, not a synthesized row."""
    assert backend_local.find_workspace_by_identity(identity="repo:nope") is None


def test_find_workspace_by_identity_returns_row_when_present(workspace_root):
    """After a create, lookup by identity returns the row with all columns."""
    backend_local.find_or_create_workspace(
        identity="repo:auth",
        slug="auth",
        name="Auth Service",
        description="OAuth2 work",
    )
    row = backend_local.find_workspace_by_identity(identity="repo:auth")
    assert row is not None
    assert row["identity"] == "repo:auth"
    assert row["slug"] == "auth"
    assert row["name"] == "Auth Service"
    assert row["description"] == "OAuth2 work"
    assert "id" in row
    assert "created_at" in row
    assert "updated_at" in row


# ── find_or_create_workspace ───────────────────────────────────────────────


def test_find_or_create_workspace_inserts_new_row(workspace_root):
    """First call with a fresh identity creates a row + returns it."""
    row = backend_local.find_or_create_workspace(
        identity="repo:billing", slug="billing", name="Billing"
    )
    assert row["identity"] == "repo:billing"
    assert row["slug"] == "billing"
    assert row["name"] == "Billing"
    assert row["description"] is None
    assert row["id"] >= 1


def test_find_or_create_workspace_is_idempotent_on_identity(workspace_root):
    """Second call with the same identity returns the SAME row id —
    does not insert a duplicate (UNIQUE constraint on `identity` would
    raise otherwise)."""
    first = backend_local.find_or_create_workspace(identity="repo:dup", slug="dup", name="Dup")
    second = backend_local.find_or_create_workspace(identity="repo:dup", slug="dup", name="Dup")
    assert first["id"] == second["id"]


def test_find_or_create_workspace_does_not_overwrite_existing_attrs(workspace_root):
    """Caller-supplied slug/name/description on a SECOND call do NOT
    overwrite the existing row. `identity` is the canonical key per §10.1;
    renames need a separate update path (not implemented here).
    """
    first = backend_local.find_or_create_workspace(
        identity="repo:rename", slug="orig-slug", name="Original", description="first"
    )
    second = backend_local.find_or_create_workspace(
        identity="repo:rename",
        slug="changed-slug",
        name="Changed",
        description="second",
    )
    assert second["id"] == first["id"]
    assert second["slug"] == "orig-slug"
    assert second["name"] == "Original"
    assert second["description"] == "first"


def test_find_or_create_workspace_accepts_no_description(workspace_root):
    """description defaults to None (TEXT nullable per the schema)."""
    row = backend_local.find_or_create_workspace(
        identity="repo:no-desc", slug="no-desc", name="No Desc"
    )
    assert row["description"] is None


# ── list_workspaces ────────────────────────────────────────────────────────


def test_list_workspaces_empty_returns_empty(workspace_root):
    """No rows seeded → empty list, NOT None."""
    assert backend_local.list_workspaces() == []


def test_list_workspaces_returns_all_rows_ordered_by_slug(workspace_root):
    """list_workspaces sorts ascending by slug per the facade contract."""
    backend_local.find_or_create_workspace(identity="repo:c", slug="charlie", name="Charlie")
    backend_local.find_or_create_workspace(identity="repo:a", slug="alpha", name="Alpha")
    backend_local.find_or_create_workspace(identity="repo:b", slug="bravo", name="Bravo")
    rows = backend_local.list_workspaces()
    assert [r["slug"] for r in rows] == ["alpha", "bravo", "charlie"]


# ── Cross-function integration ─────────────────────────────────────────────


def test_resolve_scope_now_works_in_a_real_workspace(workspace_root):
    """With the workspace stubs landed (atelier#51) AND the project +
    document stubs (atelier#52), `scripts.scope.resolve_scope` runs
    end-to-end against the real backend in a real workspace — no
    patches required. With zero projects in the workspace, the project
    slot is None and the caller (SKILL.md flow) prompts.

    Load-bearing integration test that proves #50 + #51 + #52 compose.
    """
    from scripts import scope

    s = scope.resolve_scope(workspace_override="repo:integ")
    assert s.workspace is not None
    assert s.workspace["identity"] == "repo:integ"
    assert s.workspace["slug"] == "repo-integ"
    assert s.project is None  # no projects yet — caller prompts
