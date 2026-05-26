"""Workspace + project scope resolution (spec §10).

This module is the foundation for the §10 multi-workspace model (epic #32).
It owns three concerns:

1. **Workspace identity derivation** — from CWD via `find_git_root`, with
   `git_remote_url` normalization across linked worktrees. Pure functions,
   no backend dependency.
2. **Session state** — `~/.atelier/state.json` holds per-workspace
   `current_project` pointers per §10.3. Atomic-rename writes (no partial
   writes), schema-versioned for forward-compat.
3. **`resolve_scope()`** — the §10.2 algorithm. Wires identity + session
   state + the backend workspace/project layer. The backend hooks
   (`find_or_create_workspace`, `list_projects`) are still `_not_implemented`
   stubs as of atelier#50 — the function plumbs them but will raise the
   facade's `NotImplementedError` once a git root is present. Those stubs
   land in atelier#51 and atelier#52; this module is ready for them.

The workspace-less branch (`Scope(workspace=None, project=None)` when CWD
is not in a git repo) works end-to-end today — no backend interaction.

Spec reference: `docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md`
§10.1 (layer definitions), §10.2 (detection algorithm), §10.3 (session
state), §10.4 (workspace-less ops), §10.5 (Local mode collapse).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scripts.git_utils import find_git_root, git_remote_url

_STATE_SCHEMA_VERSION = 1
_STATE_DIR_NAME = ".atelier"
_STATE_FILE_NAME = "state.json"


@dataclass(frozen=True)
class Scope:
    """Resolved scope for the current command.

    `workspace` and `project` are dict rows (matching `workspaces` /
    `projects` table shape) when populated; both None means the command
    is workspace-less per §10.4. `workspace` populated + `project` None
    is the "first run in this workspace" or "ambiguous, prompt the user"
    state — the caller is responsible for the prompt flow per §10.2.
    """

    workspace: dict | None = None
    project: dict | None = None


def _slug_from(identity: str) -> str:
    """Derive a kebab-case slug from a workspace identity string.

    Identity is either a git remote URL (e.g.
    ``git@github.com:owner/repo.git``, ``https://github.com/owner/repo``)
    or a filesystem path (e.g. ``/home/user/projects/foo-bar``). The
    slug is the lowercased repo basename with non-alphanumeric runs
    collapsed to single dashes and outer dashes stripped — matching the
    spec §10.1 examples ("auth-service", "billing", "acme-monorepo").

    Empty or whitespace-only identities return ``"workspace"`` as a
    sentinel — the caller is responsible for not feeding garbage in,
    but a deterministic non-empty slug keeps the DB UNIQUE constraint
    from misfiring on edge cases.
    """
    s = identity.strip()
    if not s:
        return "workspace"
    # Strip a trailing ``.git`` so ``foo.git`` and ``foo`` collapse.
    if s.endswith(".git"):
        s = s[:-4]
    # Take the final path-segment (handles both URL and filesystem forms).
    # ``git@host:owner/repo`` → ``owner/repo``; split on ``/`` last.
    segment = s.rstrip("/").split("/")[-1]
    # Collapse non-alphanumeric to single dashes; lowercase.
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", segment).strip("-").lower()
    return slug or "workspace"


def _derive_workspace_identity(
    *, git_root: Path | None, workspace_override: str | None
) -> tuple[str | None, str | None]:
    """Return ``(identity, slug)`` for the current scope, or ``(None, None)``
    when no workspace is detectable.

    Resolution order (highest priority first):

    1. ``workspace_override`` — CLI flag (`--workspace-id` per spec §10.2).
       When provided, both identity AND slug come from the override; no
       git inspection. Empty/whitespace override falls through.
    2. ``git_remote_url(git_root)`` — for normal git repos and linked
       worktrees with a remote configured. Identity normalizes across
       the main repo and all of its worktrees per §10.2's
       "Linked-worktree identity" note.
    3. ``str(git_root.resolve())`` — fallback for remoteless repos (and
       remoteless linked worktrees). Per §10.2: distinct paths produce
       distinct workspaces; we accept that here pending a future
       normalization decision.
    4. ``(None, None)`` — CWD is not in a git repo. Per §10.4,
       workspace-less operations are valid; the caller renders
       ``Scope(workspace=None, project=None)``.
    """
    if workspace_override is not None and workspace_override.strip():
        override = workspace_override.strip()
        return override, _slug_from(override)
    if git_root is None:
        return None, None
    remote = git_remote_url(git_root)
    if remote is not None:
        return remote, _slug_from(remote)
    # Remoteless repo OR remoteless linked worktree — distinct paths
    # become distinct workspaces per §10.2.
    path_identity = str(git_root.resolve())
    return path_identity, _slug_from(path_identity)


def _state_path() -> Path:
    """Resolve ``~/.atelier/state.json``. Indirection lets tests patch
    ``Path.home`` (or monkeypatch this function) to redirect into a
    tmp dir without mutating the real user state file."""
    return Path.home() / _STATE_DIR_NAME / _STATE_FILE_NAME


def _initial_state() -> dict:
    """A fresh state dict matching the v1 schema. Returned on (a) missing
    file, (b) corrupt JSON, (c) schema version mismatch — the read helper
    short-circuits to this rather than crashing the command."""
    return {"schema_version": _STATE_SCHEMA_VERSION, "workspaces": {}}


def read_session_state() -> dict:
    """Read ``~/.atelier/state.json`` per §10.3.

    Returns a dict matching ``_initial_state()`` shape regardless of disk
    state. Failure modes:

    - **Missing file** — return ``_initial_state()`` silently. First run.
    - **Unreadable / corrupt JSON** — emit a stderr warning, return
      ``_initial_state()``. The session continues; the corrupt file is
      left in place (NOT auto-overwritten) so the operator can inspect
      and recover any pinned project pointers manually if they want to.
    - **Schema version mismatch** — return ``_initial_state()`` and warn.
      Forward-compat: a future v2 read helper that knows how to migrate
      v1 → v2 would handle this branch differently.

    The shape mirrors §10.3's worked example: ``{"schema_version": 1,
    "workspaces": {"<workspace_id_str>": {"current_project": <project_id>,
    "set_at": "<iso-timestamp>"}}}``. Workspace ids are stored as
    JSON-object string keys per the §10.3 worked example.
    """
    path = _state_path()
    if not path.exists():
        return _initial_state()
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"atelier: warning — {path} is unreadable ({exc.__class__.__name__}); "
            f"continuing with empty session state. File left in place for inspection.",
            file=sys.stderr,
        )
        return _initial_state()
    if not isinstance(data, dict):
        print(
            f"atelier: warning — {path} root is not a JSON object; "
            f"continuing with empty session state.",
            file=sys.stderr,
        )
        return _initial_state()
    version = data.get("schema_version")
    if version != _STATE_SCHEMA_VERSION:
        print(
            f"atelier: warning — {path} schema_version={version!r} differs from "
            f"expected {_STATE_SCHEMA_VERSION}; continuing with empty session state.",
            file=sys.stderr,
        )
        return _initial_state()
    # Normalize: a missing "workspaces" subdict is treated as empty,
    # not a fatal error. This keeps the helper liberal in what it accepts.
    if not isinstance(data.get("workspaces"), dict):
        data = dict(data)
        data["workspaces"] = {}
    return data


def write_session_state(*, workspace_id: int, current_project_slug: str | None) -> None:
    """Persist ``current_project_slug`` for ``workspace_id`` to
    ``~/.atelier/state.json`` per §10.3.

    The pointer is a project SLUG (not id) to match the spec §10.1
    "Identity: (workspace_id, slug)" rule and the available facade
    method ``backend.find_project(workspace_id, slug)``. The §10.3
    worked example shows an integer id for illustration only; the
    canonical identifier is the slug because it survives project_id
    re-allocation across mode migrations (Local ↔ Memex).

    Atomic-rename write: serialize to a sibling tmp file in the same
    directory, then ``os.replace`` onto the final path. SQLite-style
    durability without a fsync — partial writes can't surface to a
    reader because the rename is atomic on POSIX (and on Windows via
    ``os.replace`` semantics).

    ``current_project_slug=None`` clears the pointer (e.g. after the
    active project is deleted). The workspace key is preserved with
    ``current_project_slug: null`` rather than removed — the latter
    would erase the ``set_at`` audit trail too. Operators can prune
    empty workspaces by hand if they want; this code never deletes
    them silently.

    Creates ``~/.atelier`` if it doesn't exist (mode 0700; user-only).
    """
    state = read_session_state()
    workspaces = state.setdefault("workspaces", {})
    key = str(workspace_id)
    workspaces[key] = {
        "current_project_slug": current_project_slug,
        "set_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    state["schema_version"] = _STATE_SCHEMA_VERSION
    path = _state_path()
    path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    # Atomic write: tmp-in-same-dir → os.replace.
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        # Best-effort cleanup of the orphan tmp file; never mask the
        # original exception.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def resolve_scope(*, workspace_override: str | None = None) -> Scope:
    """Resolve ``(workspace, project)`` for the current command per §10.2.

    Algorithm (matches the spec doc verbatim, modulo backend-stub
    wiring):

    1. ``find_git_root`` from CWD. If absent → workspace-less scope.
    2. Derive ``identity`` + ``slug`` via ``_derive_workspace_identity``,
       honoring ``workspace_override`` (CLI flag) when provided.
    3. ``backend.find_or_create_workspace(identity, slug, ...)`` →
       workspace row.
    4. Read ``~/.atelier/state.json``. If the workspace has a
       ``current_project`` pointer AND that project still exists, use it.
    5. Otherwise ``backend.list_projects(workspace_id)``:
       - 1 project → auto-select, persist the pointer, return.
       - 0 projects → caller's prompt flow (return scope with
         project=None; the SKILL.md flow handles project-creation prompts).
       - Multiple projects → caller's prompt flow (return scope with
         project=None; the SKILL.md flow handles selection).

    **Today (atelier#50):** the backend hooks `find_or_create_workspace`
    and `list_projects` are still `_not_implemented` stubs. Calling
    ``resolve_scope()`` from a git repo will raise the facade's
    ``NotImplementedError`` from step 3. The workspace-less branch
    (step 1 → return) works end-to-end. atelier#51 lands step 3's
    stub; atelier#52 lands step 5's. Wiring through this function is
    intentional — once the stubs land, the function becomes useful
    without further changes here.
    """
    git_root = find_git_root()
    identity, slug = _derive_workspace_identity(
        git_root=git_root, workspace_override=workspace_override
    )
    if identity is None or slug is None:
        return Scope(workspace=None, project=None)

    # Lazy backend import — keeps this module importable in tests that
    # only exercise the identity / state.json layers without dragging
    # the backend facade into scope.
    from scripts import backend

    workspace = backend.find_or_create_workspace(
        identity=identity,
        slug=slug,
        name=slug,  # human-displayable name defaults to slug
        description="",
    )

    state = read_session_state()
    pinned = state.get("workspaces", {}).get(str(workspace["id"]), {})
    pinned_slug = pinned.get("current_project_slug") if isinstance(pinned, dict) else None

    if pinned_slug is not None:
        project = backend.find_project(workspace_id=workspace["id"], slug=pinned_slug)
        if project is not None:
            return Scope(workspace=workspace, project=project)
        # Pointer is stale (project was deleted). Fall through to the
        # list-projects path; don't crash on a stale state.json.

    projects = backend.list_projects(workspace_id=workspace["id"])
    if len(projects) == 1:
        write_session_state(workspace_id=workspace["id"], current_project_slug=projects[0]["slug"])
        return Scope(workspace=workspace, project=projects[0])

    # 0 projects OR multiple projects: caller (SKILL.md flow) handles
    # the prompt. Return workspace-only scope; let the prompt populate
    # the project pointer.
    return Scope(workspace=workspace, project=None)
