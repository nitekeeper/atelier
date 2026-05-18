# scripts/backend.py
"""Persistence facade.

Wave 0 ships only the signatures. Wave 1 (Memex backend) and Wave 1'
(Local backend) replace the bodies with mode-dispatched implementations.

Every method is keyword-only to prevent positional-arg drift between
the two backends as they evolve. Surface mirrors spec §4.3.
"""
from __future__ import annotations
from collections.abc import Sequence
from typing import NoReturn


def _not_implemented(name: str) -> NoReturn:
    raise NotImplementedError(
        f"backend.{name} has no implementation yet. "
        f"Wave 1 (Memex) or Wave 1' (Local) supplies the body."
    )


# --- Document-shaped writes — Tier 2 -----------------------------------

def write_project(*, workspace_id: int, slug: str, name: str,
                  description: str, created_by: str) -> dict:
    """Create a project row scoped to a workspace. Returns the new row."""
    _not_implemented("write_project")


def write_document(*, workspace_id: int, project_id: int,
                   domain: str, subdomain: str | None,
                   title: str, body: str,
                   metadata: dict[str, object], caller_agent_id: str,
                   source_url: str | None = None,
                   relations: Sequence[dict] = ()) -> dict:
    """Persist a project document (design / plan / spec / etc.) and any
    declared relations. Returns the new row with index identifiers."""
    _not_implemented("write_document")


def write_task(*, workspace_id: int, project_id: int,
               title: str, description: str,
               subdomain: str | None, created_by: str,
               assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None,
               relations: Sequence[dict] = ()) -> dict:
    """Persist a task row and any declared relations. Returns the new row."""
    _not_implemented("write_task")


def write_meeting(*, workspace_id: int, project_id: int | None,
                  title: str, date: str, summary: str,
                  decisions: str, subdomain: str | None,
                  created_by: str,
                  relations: Sequence[dict] = ()) -> dict:
    """Persist a meeting record (and its markdown payload) plus relations.
    `date` is ISO YYYY-MM-DD form. Returns the new row."""
    _not_implemented("write_meeting")


# --- Operational state — Tier 1 ----------------------------------------

def upsert_session(*, project_id: int, agent_id: str, phase: str | None = None,
                   current_tasks: str | None = None,
                   accomplished: str | None = None,
                   next_action: str | None = None,
                   status: str = "in-progress",
                   pm_notes: str | None = None) -> dict:
    """Idempotent session upsert for `(project_id, agent_id)`.

    Optional fields: `phase` (current dev phase), `current_tasks` (free-form
    task summary), `accomplished` (what landed this session),
    `next_action` (planned next step), `status` (default `'in-progress'`),
    `pm_notes` (PM-visible commentary). Returns the resulting row.
    """
    _not_implemented("upsert_session")


def transition_phase(*, project_id: int, to_phase: str,
                     agent_id: str, bypass_reason: str | None = None) -> dict:
    """Advance the project phase; if a bypass is required, the caller must
    supply `bypass_reason` so `record_phase_bypass` can log it. Returns the
    new phase row."""
    _not_implemented("transition_phase")


def update_task_status(*, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    """Set the task status (e.g. 'in-progress' → 'done'). Returns the
    updated row."""
    _not_implemented("update_task_status")


def record_phase_bypass(*, project_id: int, from_phase: str, to_phase: str,
                        reason: str, agent_id: str) -> dict:
    """Log a soft-wall bypass to the `phase_bypasses` table. Returns the
    new row. Surfaced by `internal/dev-handoff` retros."""
    _not_implemented("record_phase_bypass")


# --- Workspace + project resolution ------------------------------------

def find_or_create_workspace(*, identity: str, slug: str, name: str,
                             description: str | None = None) -> dict:
    """Return the workspace row for `identity` (e.g. `repo:<git-root>`),
    creating it if absent. Idempotent."""
    _not_implemented("find_or_create_workspace")


def find_workspace_by_identity(*, identity: str) -> dict | None:
    """Return the workspace row for `identity` or None if absent."""
    _not_implemented("find_workspace_by_identity")


def list_workspaces() -> list[dict]:
    """Return every workspace row, ordered by slug."""
    _not_implemented("list_workspaces")


def find_project(*, workspace_id: int, slug: str) -> dict | None:
    """Return the project row matching `(workspace_id, slug)` or None."""
    _not_implemented("find_project")


def list_projects(*, workspace_id: int) -> list[dict]:
    """Return every project row in the workspace, ordered by slug."""
    _not_implemented("list_projects")


# --- Reads -------------------------------------------------------------

def find_documents(*, query: str, workspace_id: int | None = None,
                   project_id: int | None = None,
                   domain: str | None = None, subdomain: str | None = None,
                   limit: int = 10) -> list[dict]:
    """Full-text / metadata search over documents, optionally scoped to a
    workspace / project / domain. Returns ranked rows."""
    _not_implemented("find_documents")


def get_task(*, task_id: int) -> dict | None:
    """Return the task row for `task_id` or None if absent."""
    _not_implemented("get_task")


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    """Return every task row in the project, optionally filtered by status."""
    _not_implemented("list_tasks")


def get_document(*, doc_id: int) -> dict | None:
    """Return the document row for `doc_id` or None if absent."""
    _not_implemented("get_document")


def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Reverse-lookup for the idempotent-migration use case.

    Used by the idempotent migrator to skip rows that already landed
    during a previous partial run. Each migrated row is written with
    `metadata["source_ref"] = "atelier:<table>:<local_id>"`; on a rerun
    the migrator calls this method first, and if it returns a non-None
    index_id the row is skipped (avoiding `librarian.DuplicateKeyError`).

    Returns the Memex Index `index_id` (str) on hit; None on miss.
    """
    _not_implemented("lookup_index_id_by_source_ref")


# --- Idempotent role / agent helpers -----------------------------------
#
# Used by `scripts/seed_roles.py` (Plan 3) and the Memex-mode bootstrap.
# Both must be safe to call on a populated DB — return the existing row
# instead of raising IntegrityError.

def find_or_create_role(*, name: str, description: str) -> dict:
    """Return the role row with this `name`, creating it if absent.
    Idempotent."""
    _not_implemented("find_or_create_role")


def find_or_create_agent(*, agent_id: str, name: str, role_id: int,
                         profile: str) -> dict:
    """Return the agent row with this `agent_id`, creating it if absent.
    Idempotent."""
    _not_implemented("find_or_create_agent")
