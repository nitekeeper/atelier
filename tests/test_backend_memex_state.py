# tests/test_backend_memex_state.py
"""Tests for Plan 2 Task 2 — Memex-mode operational state writes.

These are the Tier 1 writes that bypass the Librarian path entirely:
upsert_session / transition_phase / update_task_status /
record_phase_bypass go straight through Memex Core CRUD
(scripts.stores.insert/update/query).
"""

from unittest.mock import patch

import pytest

from scripts import backend_memex


@pytest.fixture
def mock_core():
    """Patch the Memex Core dispatch helpers so tests don't need a real
    ~/.memex/atelier.db on disk."""
    with (
        patch.object(backend_memex, "_memex_core_insert") as ins,
        patch.object(backend_memex, "_memex_core_update") as upd,
        patch.object(backend_memex, "_memex_core_query") as qry,
    ):
        yield {"insert": ins, "update": upd, "query": qry}


# ── upsert_session ───────────────────────────────────────────────────────


def test_upsert_session_inserts_when_new(mock_core):
    """No in-progress session for (project_id, agent_id) → INSERT path."""

    # Query is called twice in the INSERT branch:
    #   1) sessions lookup (existing in-progress) → must be empty
    #   2) projects lookup to derive workspace_id (issue #6 bug #2)
    def query_side_effect(*, store, table, where=None):
        if table == "projects":
            return [{"id": 1, "workspace_id": 5, "slug": "myproj"}]
        return []

    mock_core["query"].side_effect = query_side_effect
    mock_core["insert"].return_value = {
        "id": 1,
        "project_id": 1,
        "agent_id": "atelier-pm-1",
        "phase": "design:open",
        "workspace_id": 5,
    }
    r = backend_memex.upsert_session(
        project_id=1,
        agent_id="atelier-pm-1",
        phase="design:open",
        current_tasks="onboarding",
        accomplished="",
        next_action="grill design",
    )
    assert r["id"] == 1
    mock_core["insert"].assert_called_once()
    # The insert must target the sessions table on the atelier store.
    kwargs = mock_core["insert"].call_args.kwargs
    assert kwargs["store"] == "atelier"
    assert kwargs["table"] == "sessions"
    # And the row body must carry the identity columns we used for lookup.
    assert kwargs["row"]["project_id"] == 1
    assert kwargs["row"]["agent_id"] == "atelier-pm-1"
    # NOT NULL workspace_id is derived from the project lookup.
    assert kwargs["row"]["workspace_id"] == 5


def test_upsert_session_updates_when_existing(mock_core):
    """An in-progress row exists → UPDATE path; insert MUST NOT fire."""
    mock_core["query"].return_value = [
        {"id": 7, "project_id": 1, "agent_id": "atelier-pm-1", "status": "in-progress"}
    ]
    mock_core["update"].return_value = {
        "id": 7,
        "project_id": 1,
        "agent_id": "atelier-pm-1",
        "accomplished": "finished kickoff",
        "status": "in-progress",
    }
    r = backend_memex.upsert_session(
        project_id=1,
        agent_id="atelier-pm-1",
        accomplished="finished kickoff",
    )
    assert r["id"] == 7
    mock_core["update"].assert_called_once()
    mock_core["insert"].assert_not_called()
    # The update must hit the existing row's id, NOT (project_id, agent_id).
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["row_id"] == 7
    assert kwargs["table"] == "sessions"


# ── transition_phase ─────────────────────────────────────────────────────


def test_transition_phase_writes_to_projects_phase_column(mock_core):
    """Project row located by id → phase column updated in the projects
    table. Spec §4.3 puts the canonical phase on projects.phase, not on
    sessions — sessions snapshot a phase for retro purposes only."""
    mock_core["query"].return_value = [{"id": 1, "phase": "design:approved"}]
    mock_core["update"].return_value = {"id": 1, "phase": "plan:open"}

    r = backend_memex.transition_phase(
        project_id=1,
        to_phase="plan:open",
        agent_id="atelier-pm-1",
    )
    assert r["phase"] == "plan:open"
    # The update target is the projects row, not sessions.
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["table"] == "projects"
    assert kwargs["row_id"] == 1
    assert kwargs["changes"] == {"phase": "plan:open"}


# ── update_task_status ───────────────────────────────────────────────────


def test_update_task_status_writes_status_column(mock_core):
    """Status update goes through Core update; the row_id is the task.id."""
    mock_core["update"].return_value = {"id": 1, "status": "in-progress"}
    r = backend_memex.update_task_status(task_id=1, status="in-progress")
    assert r["status"] == "in-progress"
    kwargs = mock_core["update"].call_args.kwargs
    assert kwargs["table"] == "tasks"
    assert kwargs["row_id"] == 1
    assert kwargs["changes"]["status"] == "in-progress"


def test_record_phase_bypass_inserts_row(mock_core):
    """Bypass logged as a new row in phase_bypasses with all five cols."""
    mock_core["insert"].return_value = {
        "id": 1,
        "project_id": 1,
        "from_phase": "design:open",
        "to_phase": "plan:open",
        "reason": "user override",
        "agent_id": "atelier-pm-1",
    }
    r = backend_memex.record_phase_bypass(
        project_id=1,
        from_phase="design:open",
        to_phase="plan:open",
        reason="user override",
        agent_id="atelier-pm-1",
    )
    assert r["id"] == 1
    kwargs = mock_core["insert"].call_args.kwargs
    assert kwargs["table"] == "phase_bypasses"
    assert kwargs["row"]["project_id"] == 1
    assert kwargs["row"]["from_phase"] == "design:open"
    assert kwargs["row"]["to_phase"] == "plan:open"
    assert kwargs["row"]["reason"] == "user override"
    assert kwargs["row"]["agent_id"] == "atelier-pm-1"


# ── list_phase_bypasses ───────────────────────────────────────────────────


def test_list_phase_bypasses_calls_memex_core_query(mock_core):
    """list_phase_bypasses must delegate to _memex_core_query with the correct
    store, table, and where kwargs."""
    mock_core["query"].return_value = []
    backend_memex.list_phase_bypasses(project_id=1)
    mock_core["query"].assert_called_once_with(
        store="atelier",
        table="phase_bypasses",
        where={"project_id": 1},
    )


# ── update_task (#26) ─────────────────────────────────────────────────────


def test_update_task_single_field_routes_through_core_update(mock_core):
    mock_core["update"].return_value = {"id": 1, "title": "New", "status": "pending"}
    r = backend_memex.update_task(task_id=1, title="New")
    assert r["title"] == "New"
    mock_core["update"].assert_called_once()
    kw = mock_core["update"].call_args.kwargs
    assert kw["store"] == "atelier"
    assert kw["table"] == "tasks"
    assert kw["row_id"] == 1
    assert kw["changes"] == {"title": "New"}


def test_update_task_multi_field(mock_core):
    mock_core["update"].return_value = {"id": 1, "title": "T", "description": "D", "priority": 4}
    backend_memex.update_task(task_id=1, title="T", description="D", priority=4)
    kw = mock_core["update"].call_args.kwargs
    assert kw["changes"] == {"title": "T", "description": "D", "priority": 4}


def test_update_task_does_not_inject_status_when_assigned_to_in_changes(mock_core):
    """Critical contract: update_task is a pure column update —
    the backend never injects status='assigned' even when the caller
    sets assigned_to. The assign_task helper is the sole flip path."""
    mock_core["update"].return_value = {"id": 1, "assigned_to": "dev-1", "status": "pending"}
    backend_memex.update_task(task_id=1, assigned_to="dev-1")
    kw = mock_core["update"].call_args.kwargs
    assert "status" not in kw["changes"]
    assert kw["changes"] == {"assigned_to": "dev-1"}


def test_update_task_no_changes_short_circuits_to_read(mock_core):
    """Empty changes → no UPDATE; falls back to a read so callers see
    the current row instead of an SQLite no-set crash."""
    mock_core["query"].return_value = [{"id": 1, "title": "old"}]
    r = backend_memex.update_task(task_id=1)
    assert r == {"id": 1, "title": "old"}
    mock_core["update"].assert_not_called()


def test_update_task_raises_when_task_missing(mock_core):
    """M1: memex must raise ValueError(f"task_id={X} not found") on
    missing rows, matching backend_local's contract. Pre-fix the empty-
    changes branch returned `{}` and the non-empty branch passed
    through `_memex_core_update` which silently returned `{}` too —
    silent-data-loss class."""
    # No row found.
    mock_core["query"].return_value = []
    with pytest.raises(ValueError, match=r"task_id=99999 not found"):
        backend_memex.update_task(task_id=99999, title="ghost")
    # Same for the empty-changes branch.
    with pytest.raises(ValueError, match=r"task_id=99999 not found"):
        backend_memex.update_task(task_id=99999)
    # _memex_core_update must NOT have been invoked for either case.
    mock_core["update"].assert_not_called()


def test_assign_task_raises_when_task_missing(mock_core):
    """M1: memex assign_task must raise ValueError on missing rows
    (same shape as backend_local). Pre-fix it forwarded straight to
    `_memex_core_update` which silently returned `{}` on miss — same
    silent-data-loss class as update_task."""
    mock_core["query"].return_value = []
    with pytest.raises(ValueError, match=r"task_id=99999 not found"):
        backend_memex.assign_task(task_id=99999, agent_id="dev-1")
    mock_core["update"].assert_not_called()


def test_update_task_rejects_status_even_when_combined_with_assigned_to(mock_core):
    """M3 memex mirror: status writes must go through update_task_status
    even when combined with other writable columns. Probe MUST NOT fire
    (raise happens before the probe)."""
    with pytest.raises(ValueError, match="status writes must go through update_task_status"):
        backend_memex.update_task(task_id=1, status="complete", assigned_to="dev-1")
    mock_core["update"].assert_not_called()


def test_update_task_with_assigned_to_only_updates_just_that_field(mock_core):
    """M3 memex mirror: passing assigned_to without status — UPDATE
    fires with only assigned_to (no facade-injected status)."""
    mock_core["query"].return_value = [{"id": 1, "status": "pending"}]
    mock_core["update"].return_value = {"id": 1, "assigned_to": "dev-1", "status": "pending"}
    backend_memex.update_task(task_id=1, assigned_to="dev-1")
    kw = mock_core["update"].call_args.kwargs
    assert kw["changes"] == {"assigned_to": "dev-1"}
    assert "status" not in kw["changes"]


def test_update_task_rejects_unknown_column_at_backend_memex(mock_core):
    """m3 defense-in-depth: a direct backend_memex caller (bypassing the
    facade) must still get a ValueError on an unknown column. Probe
    MUST NOT fire — the surface rejects before touching Memex Core."""
    with pytest.raises(ValueError, match="does not accept column"):
        backend_memex.update_task(task_id=1, evil="x")
    mock_core["update"].assert_not_called()


# ── delete_task (#27) ─────────────────────────────────────────────────────


def test_delete_task_existing_returns_true(mock_core):
    mock_core["query"].return_value = [{"id": 1}]
    with patch.object(backend_memex, "_memex_core_delete") as delete_call:
        assert backend_memex.delete_task(task_id=1) is True
        delete_call.assert_called_once()
        kw = delete_call.call_args.kwargs
        assert kw["store"] == "atelier"
        assert kw["table"] == "tasks"
        assert kw["row_id"] == 1


def test_delete_task_missing_returns_false(mock_core):
    """Probe shows row absent → False, no delete call fires."""
    mock_core["query"].return_value = []
    with patch.object(backend_memex, "_memex_core_delete") as delete_call:
        assert backend_memex.delete_task(task_id=999) is False
        delete_call.assert_not_called()


# ── assign_task (#28) ─────────────────────────────────────────────────────


def test_assign_task_single_core_update_call(mock_core):
    """Atomicity: exactly one _memex_core_update call carrying BOTH
    assigned_to and status='assigned' so the row can never be seen
    half-updated."""
    mock_core["update"].return_value = {"id": 1, "assigned_to": "dev-1", "status": "assigned"}
    r = backend_memex.assign_task(task_id=1, agent_id="dev-1")
    assert r["assigned_to"] == "dev-1"
    assert r["status"] == "assigned"
    assert mock_core["update"].call_count == 1
    kw = mock_core["update"].call_args.kwargs
    assert kw["changes"] == {"assigned_to": "dev-1", "status": "assigned"}


# ── list_tasks assigned_to filter (#29) ───────────────────────────────────


def test_list_tasks_passes_assigned_to_into_where(mock_core):
    """assigned_to rides into the where-dict so the downstream
    _memex_core_query builds a parameterized WHERE — no post-filter."""
    mock_core["query"].return_value = []
    backend_memex.list_tasks(project_id=1, assigned_to="dev-1")
    kw = mock_core["query"].call_args.kwargs
    assert kw["where"] == {"project_id": 1, "assigned_to": "dev-1"}


def test_list_tasks_combines_status_and_assigned_to(mock_core):
    mock_core["query"].return_value = []
    backend_memex.list_tasks(project_id=1, status="assigned", assigned_to="dev-1")
    kw = mock_core["query"].call_args.kwargs
    assert kw["where"] == {"project_id": 1, "status": "assigned", "assigned_to": "dev-1"}


def test_list_tasks_no_filter_only_project(mock_core):
    """No status / assigned_to → where carries just project_id."""
    mock_core["query"].return_value = []
    backend_memex.list_tasks(project_id=1)
    kw = mock_core["query"].call_args.kwargs
    assert kw["where"] == {"project_id": 1}
