"""Multi-workspace integration tests (atelier#56 / spec §10).

Final sub-issue of the §10 multi-workspace epic (#32). Closes the
acceptance criterion "Tests with ≥2 workspaces verify isolation" by
exercising every cross-workspace surface end-to-end:

- `find_project(workspace_id, slug)` resolves to the correct workspace
  even when slug collides across workspaces
- `list_projects(workspace_id)` is strictly workspace-scoped — no
  cross-workspace bleed
- `_auto_relations` workspace_id filter (atelier#30, retro-activated
  in atelier#55) prevents cross-workspace `part_of` edges when
  project_id collides
- Workspace-less reads (`workspace_id=NULL` per spec §10.4 / atelier#53)
  coexist with workspace-scoped reads
- Linked-worktree identity normalization (spec §10.2) puts main +
  linked worktrees under the SAME workspace

Local-mode tests use a real SQLite DB seeded with two workspaces;
Memex-mode tests stub `_memex_core_query` and verify the where-clause
shapes (the actual cross-workspace isolation is enforced by the SQL
predicates that `_auto_relations` and `find_project`/`list_projects`
emit, which are identical across modes).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import backend, backend_local, backend_memex, scope
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── Local-mode integration fixture ────────────────────────────────────────


@pytest.fixture
def two_workspaces(tmp_path, monkeypatch):
    """Stand up a Local-mode DB with TWO workspaces and overlapping
    project slugs / project_ids. Returns workspace ids + per-workspace
    project ids so tests can assert isolation.

    Force Local mode so dev machines with ~/.memex/ installed don't
    cross-talk via Memex Core during the test."""
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

    ws_a = backend_local.find_or_create_workspace(
        identity="repo:workspace-a", slug="workspace-a", name="Workspace A"
    )
    ws_b = backend_local.find_or_create_workspace(
        identity="repo:workspace-b", slug="workspace-b", name="Workspace B"
    )

    # Seed projects in each workspace. Note: project SLUGS COLLIDE
    # across workspaces (both workspaces have a project named "shared")
    # to verify the (workspace_id, slug) composite key is honored.
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    now = "2026-05-26T12:00Z"
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'shared', 'A-shared', '', 'design:open', 'a-1', ?, ?)",
        (ws_a["id"], now, now),
    )
    a_shared = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'a-only', 'A-only', '', 'design:open', 'a-1', ?, ?)",
        (ws_a["id"], now, now),
    )
    a_only = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'shared', 'B-shared', '', 'design:open', 'b-1', ?, ?)",
        (ws_b["id"], now, now),
    )
    b_shared = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'b-only', 'B-only', '', 'design:open', 'b-1', ?, ?)",
        (ws_b["id"], now, now),
    )
    b_only = cur.lastrowid
    conn.commit()
    conn.close()
    return {
        "root": root,
        "db": str(db),
        "ws_a": ws_a["id"],
        "ws_b": ws_b["id"],
        "a_shared": a_shared,
        "a_only": a_only,
        "b_shared": b_shared,
        "b_only": b_only,
    }


# ── find_project workspace isolation ──────────────────────────────────────


def test_find_project_resolves_correct_workspace_for_colliding_slug(two_workspaces):
    """The composite-key contract (§10.1): same slug in different
    workspaces resolves to distinct rows."""
    a_row = backend.find_project(workspace_id=two_workspaces["ws_a"], slug="shared")
    b_row = backend.find_project(workspace_id=two_workspaces["ws_b"], slug="shared")
    assert a_row is not None
    assert b_row is not None
    assert a_row["id"] == two_workspaces["a_shared"]
    assert b_row["id"] == two_workspaces["b_shared"]
    assert a_row["workspace_id"] == two_workspaces["ws_a"]
    assert b_row["workspace_id"] == two_workspaces["ws_b"]


def test_find_project_returns_none_for_other_workspace_slug(two_workspaces):
    """Slug 'a-only' exists in workspace A but NOT in B. `find_project`
    in workspace B must return None — no cross-workspace bleed."""
    assert backend.find_project(workspace_id=two_workspaces["ws_b"], slug="a-only") is None
    assert backend.find_project(workspace_id=two_workspaces["ws_a"], slug="b-only") is None


# ── list_projects workspace isolation ─────────────────────────────────────


def test_list_projects_returns_only_same_workspace_rows(two_workspaces):
    a_rows = backend.list_projects(workspace_id=two_workspaces["ws_a"])
    b_rows = backend.list_projects(workspace_id=two_workspaces["ws_b"])
    a_slugs = sorted(r["slug"] for r in a_rows)
    b_slugs = sorted(r["slug"] for r in b_rows)
    assert a_slugs == ["a-only", "shared"]
    assert b_slugs == ["b-only", "shared"]
    # No id overlap between the two result sets.
    a_ids = {r["id"] for r in a_rows}
    b_ids = {r["id"] for r in b_rows}
    assert a_ids.isdisjoint(b_ids)


# ── workspace-less + workspace-scoped read coexistence ───────────────────


def test_workspaceless_and_scoped_writes_coexist(two_workspaces):
    """Spec §10.4: workspace-less writes (NULL workspace_id) live in
    the same `project_documents` table as workspace-scoped writes
    without colliding."""
    # Workspace-less daily log (NULL workspace_id, NULL project_id).
    backend.write_document(
        workspace_id=None,
        project_id=None,
        domain="log",
        subdomain="daily",
        title="Workspace-less log",
        body="rootless content",
        metadata={},
        caller_agent_id="a-1",
    )
    # Workspace-A-scoped design doc.
    backend.write_document(
        workspace_id=two_workspaces["ws_a"],
        project_id=two_workspaces["a_only"],
        domain="design",
        subdomain="auth",
        title="A-only design",
        body="A content",
        metadata={},
        caller_agent_id="a-1",
    )
    # Read both back via SQL — verify the NULL row and the scoped row coexist.
    conn = sqlite3.connect(two_workspaces["db"])
    rows = conn.execute(
        "SELECT workspace_id, project_id, domain FROM project_documents ORDER BY id"
    ).fetchall()
    conn.close()
    assert (None, None, "log") in rows
    assert (two_workspaces["ws_a"], two_workspaces["a_only"], "design") in rows


def test_list_projects_with_null_workspace_id_raises_clearly(two_workspaces):
    """Per atelier#53 + #54, `list_projects(workspace_id=None)` is a
    category error — listing IS workspace-scoped per §10.1. Verify the
    error message is actionable."""
    with pytest.raises(ValueError, match="list_workspaces"):
        backend.list_projects(workspace_id=None)


# ── _auto_relations workspace_id filter activation (atelier#30 + #55) ─────


def test_auto_relations_workspace_filter_blocks_cross_workspace_match(monkeypatch):
    """The SQL clause `_auto_relations` emits when workspace_id is in
    metadata MUST include the workspace_id predicate. atelier#30
    wired it; atelier#55 retro-activated it by injecting workspace_id
    in `_atelier_write`. Verify the predicate is in the actual SQL by
    spying on `_memex_module("stores").query` calls."""
    monkeypatch.undo()  # release conftest stub of the singleton workspace
    queries_seen: list = []

    class FakeStores:
        @staticmethod
        def query(store, sql, params):
            queries_seen.append((store, sql, params))
            return []

    monkeypatch.setattr(
        backend_memex,
        "_memex_module",
        lambda name: FakeStores if name == "stores" else None,
    )
    # Drive _auto_relations directly with metadata that carries
    # workspace_id (this is what `_atelier_write`'s injection step
    # would do for every write post-#55).
    backend_memex._auto_relations(metadata={"project_id": 42, "workspace_id": 7}, explicit=[])
    assert len(queries_seen) == 1
    _store, sql, params = queries_seen[0]
    assert "$.workspace_id" in sql, (
        "atelier#30 workspace_id predicate missing from _auto_relations SQL — filter is NOT active"
    )
    assert "$.project_id" in sql
    # Params order: ("project", project_id, workspace_id).
    assert params == ("project", 42, 7)


def test_auto_relations_omits_workspace_filter_when_metadata_lacks_id(monkeypatch):
    """Back-compat: when metadata has project_id but NO workspace_id
    (e.g. a pre-#55 caller, or a workspace-less write), the SQL must
    omit the workspace_id predicate. atelier#55 made workspace_id
    auto-injection the dominant path, but the no-workspace_id branch
    is still exercised."""
    monkeypatch.undo()
    queries_seen: list = []

    class FakeStores:
        @staticmethod
        def query(store, sql, params):
            queries_seen.append((store, sql, params))
            return []

    monkeypatch.setattr(
        backend_memex,
        "_memex_module",
        lambda name: FakeStores if name == "stores" else None,
    )
    backend_memex._auto_relations(metadata={"project_id": 42}, explicit=[])
    assert len(queries_seen) == 1
    _store, sql, _params = queries_seen[0]
    assert "$.workspace_id" not in sql


# ── linked-worktree identity (spec §10.2) ────────────────────────────────


def test_linked_worktree_identity_normalizes_to_same_workspace(tmp_path):
    """Spec §10.2 "Linked-worktree identity": main repo + linked
    worktree share `git_remote_url`, so `_derive_workspace_identity`
    returns the SAME identity. `find_or_create_workspace` is
    idempotent on identity — the linked worktree must land in the
    SAME workspace row, not create a duplicate.

    End-to-end integration of atelier#50 (scope.py) + #51 (workspace
    stubs) covering the spec's normalization rule for linked
    worktrees with a configured remote.
    """
    from scripts import mode_detector

    # Force Local mode so this test doesn't depend on a real Memex install.
    main = tmp_path / "main-checkout"
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "remote",
            "add",
            "origin",
            "https://github.com/me/shared-linked.git",
        ],
        check=True,
        capture_output=True,
    )
    (main / "seed").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "seed"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
    )
    linked = tmp_path / "linked-checkout"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "wt-branch", str(linked)],
        check=True,
        capture_output=True,
    )

    # Stand up an atelier.db in the MAIN checkout. Local-mode backend
    # resolves CWD via find_git_root; both the main and linked
    # worktrees will resolve to the same workspace identity (the
    # remote URL) via scope._derive_workspace_identity, and
    # find_or_create_workspace is idempotent on identity.
    main_db = main / ".ai" / "atelier.db"
    main_db.parent.mkdir()
    apply_migrations(str(main_db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(main_db), MIGRATIONS_DIR / "local-only")

    main_identity, main_slug = scope._derive_workspace_identity(
        git_root=main, workspace_override=None
    )
    linked_identity, linked_slug = scope._derive_workspace_identity(
        git_root=linked, workspace_override=None
    )
    # The normalization rule: both checkouts produce the SAME identity
    # because git_remote_url returns the same value for both.
    assert main_identity == linked_identity == "https://github.com/me/shared-linked.git"
    assert main_slug == linked_slug == "shared-linked"

    # Now create a workspace from each identity (via the facade) and
    # verify they collapse to the same row id — idempotent on identity.
    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch("scripts.backend_local._workspace_root", return_value=main),
    ):
        from_main = backend.find_or_create_workspace(
            identity=main_identity, slug=main_slug, name=main_slug
        )
        from_linked = backend.find_or_create_workspace(
            identity=linked_identity, slug=linked_slug, name=linked_slug
        )
    assert from_main["id"] == from_linked["id"]
    assert from_main["identity"] == "https://github.com/me/shared-linked.git"


# ── Memex-mode parity (mocked) ────────────────────────────────────────────


def test_memex_mode_find_project_uses_composite_key_predicate(monkeypatch):
    """Verify the Memex backend's `find_project` emits the
    `(workspace_id, slug)` composite where-clause — the multi-workspace
    isolation IS the SQL predicate, identical to Local mode."""
    monkeypatch.undo()  # release conftest stubs of _singleton_workspace etc.
    seen: dict = {}

    def fake_query(*, store, table, where=None):
        seen["where"] = where
        return [{"id": 7, "workspace_id": 1, "slug": "shared"}]

    monkeypatch.setattr(backend_memex, "_memex_core_query", fake_query)
    backend_memex.find_project(workspace_id=1, slug="shared")
    assert seen["where"] == {"workspace_id": 1, "slug": "shared"}


def test_memex_mode_list_projects_uses_workspace_predicate(monkeypatch):
    monkeypatch.undo()
    seen: dict = {}

    def fake_query(*, store, table, where=None):
        seen["where"] = where
        return []

    monkeypatch.setattr(backend_memex, "_memex_core_query", fake_query)
    backend_memex.list_projects(workspace_id=42)
    assert seen["where"] == {"workspace_id": 42}
