"""Tests for `scripts/scope.py` (atelier#50 / spec §10).

Covers the three concerns of the module independently:

1. **Identity derivation** (`_derive_workspace_identity`, `_slug_from`) —
   pure functions, no I/O.
2. **Session state** (`read_session_state`, `write_session_state`) —
   filesystem I/O against ``~/.atelier/state.json`` (redirected to
   tmp_path via patching ``_state_path``).
3. **`resolve_scope()`** — end-to-end algorithm. Backend stubs are
   monkeypatched (they're `_not_implemented` until atelier#51/#52
   land); workspace-less branch (no git) is tested unmocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import scope

# ──────────────────────────────────────────────────────────────────────────
# _slug_from — pure function, no fixtures needed
# ──────────────────────────────────────────────────────────────────────────


def test_slug_from_https_url():
    assert scope._slug_from("https://github.com/owner/auth-service") == "auth-service"


def test_slug_from_ssh_url():
    assert scope._slug_from("git@github.com:owner/billing.git") == "billing"


def test_slug_from_strips_trailing_dot_git():
    # ".git" suffix is dropped before slugifying so "foo.git" and "foo"
    # collapse to the same slug.
    assert scope._slug_from("https://github.com/owner/foo.git") == "foo"


def test_slug_from_filesystem_path():
    assert scope._slug_from("/home/user/projects/acme-monorepo") == "acme-monorepo"


def test_slug_from_collapses_special_chars():
    # "Foo Bar_2" → lowercase, non-alphanumeric → dash, collapsed.
    assert scope._slug_from("Foo Bar_2") == "foo-bar-2"


def test_slug_from_empty_returns_sentinel():
    assert scope._slug_from("") == "workspace"
    assert scope._slug_from("   ") == "workspace"


def test_slug_from_pure_punctuation_returns_sentinel():
    # "___" → after non-alphanumeric collapse + strip → empty → sentinel.
    assert scope._slug_from("___") == "workspace"


# ──────────────────────────────────────────────────────────────────────────
# _derive_workspace_identity — covers the four resolution paths
# ──────────────────────────────────────────────────────────────────────────


def test_identity_override_wins_over_git(tmp_path):
    """CLI --workspace-id override beats any git inspection."""
    identity, slug = scope._derive_workspace_identity(
        git_root=tmp_path, workspace_override="my-custom-id"
    )
    assert identity == "my-custom-id"
    assert slug == "my-custom-id"


def test_identity_override_empty_falls_through(tmp_path):
    """Empty / whitespace override falls through to git inspection."""
    # No git remote in tmp_path; should hit the path-fallback branch.
    (tmp_path / ".git").mkdir()
    identity, slug = scope._derive_workspace_identity(git_root=tmp_path, workspace_override="   ")
    assert identity == str(tmp_path.resolve())
    # Slug runs through `_slug_from`, which collapses underscores → dashes.
    assert slug == scope._slug_from(tmp_path.name)


def test_identity_uses_git_remote_when_present(tmp_path):
    """`git_remote_url` value becomes the identity."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/me/test-repo.git",
        ],
        check=True,
        capture_output=True,
    )
    identity, slug = scope._derive_workspace_identity(git_root=tmp_path, workspace_override=None)
    assert identity == "https://github.com/me/test-repo.git"
    assert slug == "test-repo"


def test_identity_falls_back_to_path_for_remoteless_repo(tmp_path):
    """Remoteless git repo → identity is the resolved git_root path."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    identity, slug = scope._derive_workspace_identity(git_root=tmp_path, workspace_override=None)
    assert identity == str(tmp_path.resolve())
    assert slug == scope._slug_from(tmp_path.name)


def test_identity_normalizes_across_linked_worktree_with_remote(tmp_path):
    """Spec §10.2 "Linked-worktree identity": when a remote is configured,
    the main repo and a linked worktree resolve to the SAME workspace
    identity (the remote URL)."""
    main = tmp_path / "main"
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main), "remote", "add", "origin", "https://github.com/me/shared.git"],
        check=True,
        capture_output=True,
    )
    # Need at least one commit before `git worktree add` will accept a new
    # branch name. Use a minimal commit on the default branch.
    (main / "README").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "README"], check=True, capture_output=True)
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
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "wt-branch", str(linked)],
        check=True,
        capture_output=True,
    )
    main_identity, main_slug = scope._derive_workspace_identity(
        git_root=main, workspace_override=None
    )
    linked_identity, linked_slug = scope._derive_workspace_identity(
        git_root=linked, workspace_override=None
    )
    # Both checkouts produce the SAME identity because both resolve
    # `origin` to the same URL — this is the spec §10.2 normalization rule.
    assert main_identity == linked_identity == "https://github.com/me/shared.git"
    assert main_slug == linked_slug == "shared"


def test_identity_does_not_normalize_remoteless_linked_worktree(tmp_path):
    """Spec §10.2: remoteless linked worktrees fall through to the
    path-identity branch and therefore appear as DISTINCT workspaces
    from their main repo. This is the accepted-as-is behavior pending a
    future normalization decision."""
    main = tmp_path / "main"
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    (main / "README").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "README"], check=True, capture_output=True)
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
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "wt-branch", str(linked)],
        check=True,
        capture_output=True,
    )
    main_identity, _ = scope._derive_workspace_identity(git_root=main, workspace_override=None)
    linked_identity, _ = scope._derive_workspace_identity(git_root=linked, workspace_override=None)
    # No remote → distinct paths → distinct identities.
    assert main_identity == str(main.resolve())
    assert linked_identity == str(linked.resolve())
    assert main_identity != linked_identity


def test_identity_none_when_no_git_root():
    """Workspace-less branch — caller renders Scope(workspace=None, project=None)."""
    identity, slug = scope._derive_workspace_identity(git_root=None, workspace_override=None)
    assert identity is None
    assert slug is None


# ──────────────────────────────────────────────────────────────────────────
# state.json read/write
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect ``_state_path`` to a tmp dir for the duration of the test."""
    state_dir = tmp_path / "atelier_state"
    state_dir.mkdir()
    target = state_dir / "state.json"
    monkeypatch.setattr(scope, "_state_path", lambda: target)
    return target


def test_read_session_state_missing_file_returns_initial(tmp_state):
    state = scope.read_session_state()
    assert state == {"schema_version": 1, "workspaces": {}}


def test_read_session_state_returns_persisted_pointer(tmp_state):
    tmp_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspaces": {
                    "1": {"current_project_slug": "oauth-rewrite", "set_at": "2026-05-26T00:00:00Z"}
                },
            }
        ),
        encoding="utf-8",
    )
    state = scope.read_session_state()
    assert state["workspaces"]["1"]["current_project_slug"] == "oauth-rewrite"


def test_read_session_state_corrupt_json_warns_and_returns_initial(tmp_state, capsys):
    tmp_state.write_text("{this is not json", encoding="utf-8")
    state = scope.read_session_state()
    assert state == {"schema_version": 1, "workspaces": {}}
    err = capsys.readouterr().err
    assert "unreadable" in err
    # Corrupt file is left in place for operator inspection (NOT overwritten).
    assert tmp_state.read_text(encoding="utf-8") == "{this is not json"


def test_read_session_state_schema_mismatch_warns_and_returns_initial(tmp_state, capsys):
    tmp_state.write_text(json.dumps({"schema_version": 99, "workspaces": {}}), encoding="utf-8")
    state = scope.read_session_state()
    assert state == {"schema_version": 1, "workspaces": {}}
    assert "schema_version=99" in capsys.readouterr().err


def test_read_session_state_non_object_root_warns(tmp_state, capsys):
    tmp_state.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    state = scope.read_session_state()
    assert state == {"schema_version": 1, "workspaces": {}}
    assert "not a JSON object" in capsys.readouterr().err


def test_write_session_state_creates_file_atomically(tmp_state):
    scope.write_session_state(workspace_id=1, current_project_slug="oauth-rewrite")
    assert tmp_state.exists()
    data = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["workspaces"]["1"]["current_project_slug"] == "oauth-rewrite"
    assert "set_at" in data["workspaces"]["1"]


def test_write_session_state_clears_pointer(tmp_state):
    """current_project_slug=None preserves the workspace key + set_at audit
    trail but nulls the pointer."""
    scope.write_session_state(workspace_id=1, current_project_slug="oauth-rewrite")
    scope.write_session_state(workspace_id=1, current_project_slug=None)
    data = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert data["workspaces"]["1"]["current_project_slug"] is None
    assert "set_at" in data["workspaces"]["1"]


def test_write_session_state_preserves_other_workspaces(tmp_state):
    """Writing workspace 2 must not clobber workspace 1's pointer."""
    scope.write_session_state(workspace_id=1, current_project_slug="proj-a")
    scope.write_session_state(workspace_id=2, current_project_slug="proj-b")
    data = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert data["workspaces"]["1"]["current_project_slug"] == "proj-a"
    assert data["workspaces"]["2"]["current_project_slug"] == "proj-b"


def test_write_session_state_no_tmp_leftover_on_success(tmp_state):
    """The atomic-rename pattern should not leave .state-*.json.tmp
    siblings after a successful write."""
    scope.write_session_state(workspace_id=1, current_project_slug="proj-a")
    leftovers = list(tmp_state.parent.glob(".state-*.json.tmp"))
    assert leftovers == []


def test_write_session_state_file_permission_is_user_only(tmp_state):
    """Atomic-rename writes finalize with mode 0600 (user-only) so the
    state file doesn't expose project pointers to other local users."""
    scope.write_session_state(workspace_id=1, current_project_slug="proj-a")
    mode = tmp_state.stat().st_mode & 0o777
    assert mode == 0o600


# ──────────────────────────────────────────────────────────────────────────
# resolve_scope() — end-to-end (workspace-less branch unmocked; git
# branches use backend mocks until atelier#51/#52 land)
# ──────────────────────────────────────────────────────────────────────────


def test_resolve_scope_returns_workspaceless_when_no_git(tmp_path, monkeypatch):
    """CWD outside any git repo → Scope(workspace=None, project=None).
    This branch works end-to-end TODAY (no backend interaction)."""
    monkeypatch.chdir(tmp_path)
    s = scope.resolve_scope()
    assert s.workspace is None
    assert s.project is None


def test_resolve_scope_returns_workspaceless_when_git_root_is_none(monkeypatch):
    """Belt-and-suspenders: even if CWD does have git, mock find_git_root
    to None and confirm we short-circuit to workspaceless."""
    monkeypatch.setattr(scope, "find_git_root", lambda: None)
    s = scope.resolve_scope()
    assert s == scope.Scope(workspace=None, project=None)


def test_resolve_scope_uses_workspace_override_when_provided(monkeypatch, tmp_state):
    """--workspace-id override must drive identity even if CWD is non-git."""
    # No git_root, but the override should bypass that check (well, kinda —
    # the override only kicks in when git_root IS resolved; verify the
    # composite behaviour by simulating a git root present + override.)
    monkeypatch.setattr(scope, "find_git_root", lambda: Path("/fake/root"))
    monkeypatch.setattr(scope, "git_remote_url", lambda root: None)

    seen = {}

    def fake_find_or_create_workspace(*, identity, slug, name, description):
        seen["identity"] = identity
        seen["slug"] = slug
        return {"id": 42, "identity": identity, "slug": slug, "name": name}

    def fake_list_projects(*, workspace_id):
        return []

    from scripts import backend

    monkeypatch.setattr(backend, "find_or_create_workspace", fake_find_or_create_workspace)
    monkeypatch.setattr(backend, "list_projects", fake_list_projects)

    s = scope.resolve_scope(workspace_override="my-custom-ws")
    assert seen["identity"] == "my-custom-ws"
    assert seen["slug"] == "my-custom-ws"
    assert s.workspace is not None
    assert s.workspace["id"] == 42
    assert s.project is None  # zero projects → caller prompts


def test_resolve_scope_auto_selects_sole_project(monkeypatch, tmp_state):
    """When the workspace has exactly one project, resolve_scope persists
    the slug pointer and returns the project."""
    monkeypatch.setattr(scope, "find_git_root", lambda: Path("/fake/root"))
    monkeypatch.setattr(scope, "git_remote_url", lambda root: "https://github.com/me/repo")

    ws = {"id": 7, "slug": "repo", "identity": "https://github.com/me/repo"}
    sole_project = {"id": 1, "slug": "main", "workspace_id": 7}

    from scripts import backend

    monkeypatch.setattr(backend, "find_or_create_workspace", lambda **kw: ws)
    monkeypatch.setattr(backend, "list_projects", lambda *, workspace_id: [sole_project])

    s = scope.resolve_scope()
    assert s.workspace == ws
    assert s.project == sole_project

    # The pointer must have been persisted by slug, not id.
    state = scope.read_session_state()
    assert state["workspaces"]["7"]["current_project_slug"] == "main"


def test_resolve_scope_uses_pinned_project_when_state_has_pointer(monkeypatch, tmp_state):
    """If state.json carries a current_project_slug AND find_project
    returns a row, resolve_scope returns that project without listing."""
    monkeypatch.setattr(scope, "find_git_root", lambda: Path("/fake/root"))
    monkeypatch.setattr(scope, "git_remote_url", lambda root: "https://github.com/me/repo")

    ws = {"id": 7, "slug": "repo", "identity": "https://github.com/me/repo"}
    pinned = {"id": 5, "slug": "feature-x", "workspace_id": 7}

    # Pre-seed state.json with the pinned slug.
    scope.write_session_state(workspace_id=7, current_project_slug="feature-x")

    from scripts import backend

    monkeypatch.setattr(backend, "find_or_create_workspace", lambda **kw: ws)
    monkeypatch.setattr(
        backend,
        "find_project",
        lambda *, workspace_id, slug: pinned if slug == "feature-x" else None,
    )

    # Sentinel: list_projects MUST NOT be called when a pinned slug resolves.
    list_called = {"count": 0}

    def fake_list_projects(**kw):
        list_called["count"] += 1
        return []

    monkeypatch.setattr(backend, "list_projects", fake_list_projects)

    s = scope.resolve_scope()
    assert s.workspace == ws
    assert s.project == pinned
    assert list_called["count"] == 0, "find_project hit; list_projects must NOT be called"


def test_resolve_scope_falls_through_stale_pointer(monkeypatch, tmp_state):
    """If state.json has a pointer but find_project returns None (stale),
    fall through to list_projects rather than crashing."""
    monkeypatch.setattr(scope, "find_git_root", lambda: Path("/fake/root"))
    monkeypatch.setattr(scope, "git_remote_url", lambda root: "https://github.com/me/repo")

    ws = {"id": 7, "slug": "repo", "identity": "https://github.com/me/repo"}
    scope.write_session_state(workspace_id=7, current_project_slug="deleted-proj")

    from scripts import backend

    monkeypatch.setattr(backend, "find_or_create_workspace", lambda **kw: ws)
    monkeypatch.setattr(backend, "find_project", lambda *, workspace_id, slug: None)
    monkeypatch.setattr(backend, "list_projects", lambda *, workspace_id: [])

    s = scope.resolve_scope()
    assert s.workspace == ws
    assert s.project is None  # 0 projects, caller prompts


def test_resolve_scope_returns_workspace_only_when_multiple_projects(monkeypatch, tmp_state):
    """Multiple projects with no pinned pointer → workspace populated but
    project=None; the SKILL.md flow prompts the user to pick."""
    monkeypatch.setattr(scope, "find_git_root", lambda: Path("/fake/root"))
    monkeypatch.setattr(scope, "git_remote_url", lambda root: "https://github.com/me/repo")

    ws = {"id": 7, "slug": "repo", "identity": "https://github.com/me/repo"}
    projects = [
        {"id": 1, "slug": "feature-a", "workspace_id": 7},
        {"id": 2, "slug": "feature-b", "workspace_id": 7},
    ]

    from scripts import backend

    monkeypatch.setattr(backend, "find_or_create_workspace", lambda **kw: ws)
    monkeypatch.setattr(backend, "list_projects", lambda *, workspace_id: projects)

    s = scope.resolve_scope()
    assert s.workspace == ws
    assert s.project is None  # ambiguous, caller prompts
