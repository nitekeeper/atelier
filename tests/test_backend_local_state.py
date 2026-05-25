"""Plan 2 Task 6 — Local-mode operational state writes.

Tests `backend_local.upsert_session` / `transition_phase` /
`update_task_status` / `record_phase_bypass` against the v1.1.0 schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed(db_path: str) -> dict:
    """Seed workspaces + roles + agents + project + task."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Product Manager", "PM", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("atelier-pm-1", "PM", role_id, "pm", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (proj_id, "Fix bug", "desc", "pending", "atelier-pm-1", now, now),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"workspace_id": ws_id, "project_id": proj_id, "task_id": task_id}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ids = _seed(str(db))
    return {"root": root, "db": str(db), **ids}


# ── upsert_session ─────────────────────────────────────────────────────────


def test_upsert_session_inserts_when_new(workspace):
    s = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        phase="design:open",
    )
    assert s["id"] >= 1
    assert s["phase"] == "design:open"
    assert s["agent_id"] == "atelier-pm-1"
    assert s["status"] == "in-progress"


def test_upsert_session_updates_when_existing(workspace):
    first = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        phase="design:open",
    )
    second = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        accomplished="kickoff done",
    )
    # Same row updated, not a new insert.
    assert second["id"] == first["id"]
    assert second["accomplished"] == "kickoff done"
    # phase from first call is preserved.
    assert second["phase"] == "design:open"


# ── transition_phase ───────────────────────────────────────────────────────


def test_transition_phase_updates_projects_phase(workspace):
    """`transition_phase` updates `projects.phase` (NOT sessions.phase —
    the previous test name implied otherwise; corrected per C2 review).
    Lock the SQL contract: after the call, the projects row reads the
    new phase."""
    r = backend_local.transition_phase(
        project_id=workspace["project_id"],
        to_phase="plan:open",
        agent_id="atelier-pm-1",
    )
    assert r["phase"] == "plan:open"
    # Confirm DB-level write on the projects table.
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT phase FROM projects WHERE id = ?",
        (workspace["project_id"],),
    ).fetchone()
    conn.close()
    assert row["phase"] == "plan:open"


def test_transition_phase_raises_when_project_missing(workspace):
    """Nit-4: error contracts must be uniform — `upsert_session` raises
    on missing project_id, so `transition_phase` does too. Was silently
    returning {} pre-fix."""
    with pytest.raises(ValueError):
        backend_local.transition_phase(
            project_id=99999,
            to_phase="plan:open",
            agent_id="atelier-pm-1",
        )


# ── update_task_status ─────────────────────────────────────────────────────


def test_update_task_status_writes_status_and_timestamps(workspace):
    """Exercise both COALESCE branches: claimed_at on first 'in-progress'
    and completed_at on first 'complete'. Idempotent re-complete must not
    overwrite the timestamp (Imp-1 from QA)."""
    # First flip: pending → in-progress sets claimed_at.
    r1 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="in-progress",
    )
    assert r1["status"] == "in-progress"
    assert r1["claimed_at"] is not None
    assert r1["updated_at"] is not None
    first_claimed_at = r1["claimed_at"]

    # Second flip: in-progress → complete sets completed_at; claimed_at unchanged.
    r2 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="complete",
    )
    assert r2["status"] == "complete"
    assert r2["completed_at"] is not None
    assert r2["claimed_at"] == first_claimed_at  # COALESCE preserved
    first_completed_at = r2["completed_at"]

    # Idempotent re-complete: completed_at must NOT change.
    r3 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="complete",
    )
    assert r3["completed_at"] == first_completed_at


def test_update_task_status_raises_when_task_missing(workspace):
    """Nit-4: error contract uniformity (mirror transition_phase)."""
    with pytest.raises(ValueError):
        backend_local.update_task_status(task_id=99999, status="complete")


# ── record_phase_bypass ────────────────────────────────────────────────────


def test_record_phase_bypass_inserts_row(workspace):
    r = backend_local.record_phase_bypass(
        project_id=workspace["project_id"],
        from_phase="design:open",
        to_phase="plan:open",
        reason="override",
        agent_id="atelier-pm-1",
    )
    assert r["id"] >= 1
    assert r["reason"] == "override"
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM phase_bypasses WHERE id = ?", (r["id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["from_phase"] == "design:open"
    assert row["to_phase"] == "plan:open"
    assert row["agent_id"] == "atelier-pm-1"


# ── list_phase_bypasses ────────────────────────────────────────────────────


def test_list_phase_bypasses_empty_returns_empty_list(workspace):
    """No bypasses recorded → list returns []."""
    result = backend_local.list_phase_bypasses(project_id=workspace["project_id"])
    assert result == []


def test_list_phase_bypasses_filters_by_project_id(workspace):
    """Safety F4 mandatory: two projects seeded with different bypass counts;
    list_phase_bypasses must return only rows for the requested project_id."""
    # Seed a second project so we can assert cross-project isolation.
    now = "2026-05-23T00:00:00Z"
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    ws_id = conn.execute(
        "SELECT workspace_id FROM projects WHERE id = ?", (workspace["project_id"],)
    ).fetchone()["workspace_id"]
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "proj-b", "ProjB", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_b_id = cur.lastrowid
    conn.commit()
    conn.close()

    proj_a_id = workspace["project_id"]

    # Seed 2 bypasses for project A, 3 for project B.
    for i in range(2):
        backend_local.record_phase_bypass(
            project_id=proj_a_id,
            from_phase="design:open",
            to_phase="plan:open",
            reason=f"reason-a-{i}",
            agent_id="atelier-pm-1",
        )
    for i in range(3):
        backend_local.record_phase_bypass(
            project_id=proj_b_id,
            from_phase="design:open",
            to_phase="plan:open",
            reason=f"reason-b-{i}",
            agent_id="atelier-pm-1",
        )

    rows_a = backend_local.list_phase_bypasses(project_id=proj_a_id)
    assert len(rows_a) == 2
    assert all(r["project_id"] == proj_a_id for r in rows_a)


def test_list_phase_bypasses_returns_v110_columns(workspace):
    """Returned dicts must contain exactly the v1.1.0 phase_bypasses columns."""
    backend_local.record_phase_bypass(
        project_id=workspace["project_id"],
        from_phase="design:open",
        to_phase="plan:open",
        reason="testing",
        agent_id="atelier-pm-1",
    )
    rows = backend_local.list_phase_bypasses(project_id=workspace["project_id"])
    assert len(rows) == 1
    expected_keys = {
        "id",
        "project_id",
        "from_phase",
        "to_phase",
        "reason",
        "agent_id",
        "created_at",
    }
    assert set(rows[0].keys()) == expected_keys


# ── update_task ────────────────────────────────────────────────────────────
#
# Issue #26: general partial update — single/multi-field; status not
# auto-flipped when assigned_to is in the changes; raises on miss.


def test_update_task_single_field(workspace):
    """Single column write — only that column moves; status is untouched."""
    r = backend_local.update_task(
        task_id=workspace["task_id"],
        title="New Title",
    )
    assert r["title"] == "New Title"
    # Status was seeded as 'pending' by _seed() — must remain pending,
    # because update_task does NOT auto-flip status.
    assert r["status"] == "pending"


def test_update_task_multi_field(workspace):
    """Multi-column write — all changes applied in one call."""
    r = backend_local.update_task(
        task_id=workspace["task_id"],
        title="T",
        description="D",
        priority=4,
        notes="n",
    )
    assert r["title"] == "T"
    assert r["description"] == "D"
    assert r["priority"] == 4
    assert r["notes"] == "n"
    # No auto-flip; status remains as seeded.
    assert r["status"] == "pending"


def test_update_task_does_not_auto_flip_status_when_assigned_to_changes(workspace):
    """Critical contract from issue #26: assigning via update_task must
    NOT touch status. Only the dedicated assign_task helper flips it."""
    r = backend_local.update_task(
        task_id=workspace["task_id"],
        assigned_to="atelier-pm-1",
    )
    assert r["assigned_to"] == "atelier-pm-1"
    # Seeded as 'pending'; update_task is a pure column update.
    assert r["status"] == "pending"


def test_update_task_raises_when_task_missing(workspace):
    with pytest.raises(ValueError):
        backend_local.update_task(task_id=99999, title="ghost")


def test_update_task_rejects_status_even_when_combined_with_assigned_to(workspace):
    """M3: backend.update_task rejects `status` because it would bypass the
    COALESCE timestamp side-effects in update_task_status. Defense-in-depth
    mirror is enforced here too — even mixing assigned_to in the same call
    doesn't smuggle the status write through."""
    with pytest.raises(ValueError, match="status writes must go through update_task_status"):
        backend_local.update_task(
            task_id=workspace["task_id"],
            status="complete",
            assigned_to="atelier-pm-1",
        )


def test_update_task_with_assigned_to_only_updates_just_that_field(workspace):
    """M3: passing `assigned_to` without `status` succeeds and leaves the
    seeded status ('pending') untouched — confirms no auto-flip."""
    before = backend_local.get_task(task_id=workspace["task_id"])
    assert before["status"] == "pending"
    r = backend_local.update_task(
        task_id=workspace["task_id"],
        assigned_to="atelier-pm-1",
    )
    assert r["assigned_to"] == "atelier-pm-1"
    # Status MUST remain pending — pure column update.
    assert r["status"] == before["status"]


def test_update_task_rejects_unknown_column_at_backend_local(workspace):
    """m3 defense-in-depth: a direct backend_local caller (bypassing the
    facade) must still get a ValueError on an unknown column."""
    with pytest.raises(ValueError, match="does not accept column"):
        backend_local.update_task(task_id=workspace["task_id"], evil="x")


# ── delete_task ────────────────────────────────────────────────────────────


def test_delete_task_existing_returns_true(workspace):
    """Issue #27: deleting a real task returns True and removes the row."""
    assert backend_local.delete_task(task_id=workspace["task_id"]) is True
    assert backend_local.get_task(task_id=workspace["task_id"]) is None


def test_delete_task_missing_returns_false(workspace):
    """Issue #27: rowcount-based contract — absent id returns False."""
    assert backend_local.delete_task(task_id=99999) is False


def test_delete_task_is_idempotent(workspace):
    """Two calls in a row: first deletes (True), second is a no-op (False)."""
    assert backend_local.delete_task(task_id=workspace["task_id"]) is True
    assert backend_local.delete_task(task_id=workspace["task_id"]) is False


# ── assign_task ────────────────────────────────────────────────────────────


def test_assign_task_sets_both_fields(workspace):
    """Issue #28: post-call both assigned_to and status are set."""
    r = backend_local.assign_task(task_id=workspace["task_id"], agent_id="atelier-pm-1")
    assert r["assigned_to"] == "atelier-pm-1"
    assert r["status"] == "assigned"


def test_assign_task_uses_single_update_statement(workspace, monkeypatch):
    """Atomicity check: a single backend call → a single connection
    that issues exactly one UPDATE on the tasks table. We instrument
    `sqlite3.Connection.execute` via a wrapping monkeypatch on the
    `_conn()` helper so we can count UPDATE-tasks statements through
    one assign_task call."""
    update_calls = []
    real_conn = backend_local._conn

    class _Wrap:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("UPDATE TASKS"):
                update_calls.append(sql)
            return self._c.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._c, name)

    def fake_conn():
        return _Wrap(real_conn())

    monkeypatch.setattr(backend_local, "_conn", fake_conn)
    backend_local.assign_task(task_id=workspace["task_id"], agent_id="atelier-pm-1")
    assert len(update_calls) == 1, (
        f"assign_task must issue exactly one UPDATE on tasks; got {update_calls}"
    )


def test_assign_task_raises_when_task_missing(workspace):
    with pytest.raises(ValueError):
        backend_local.assign_task(task_id=99999, agent_id="atelier-pm-1")


# ── list_tasks assigned_to filter ──────────────────────────────────────────


def test_list_tasks_assigned_to_filter_narrows_results(workspace):
    """Issue #29: the filter is pushed into the WHERE clause."""
    proj_id = workspace["project_id"]
    # Seed a second task assigned to a different agent.
    now = "2026-05-23T00:00:00Z"
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "assigned_to, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (proj_id, "T2", "d", "assigned", "dev-2", "atelier-pm-1", now, now),
    )
    conn.commit()
    conn.close()

    # Assign the seeded task to dev-1.
    backend_local.assign_task(task_id=workspace["task_id"], agent_id="dev-1")

    only_dev1 = backend_local.list_tasks(project_id=proj_id, assigned_to="dev-1")
    only_dev2 = backend_local.list_tasks(project_id=proj_id, assigned_to="dev-2")
    assert len(only_dev1) == 1
    assert only_dev1[0]["assigned_to"] == "dev-1"
    assert len(only_dev2) == 1
    assert only_dev2[0]["assigned_to"] == "dev-2"


def test_list_tasks_no_filter_returns_all(workspace):
    """No assigned_to filter — all rows in the project come back."""
    proj_id = workspace["project_id"]
    backend_local.assign_task(task_id=workspace["task_id"], agent_id="dev-1")
    rows = backend_local.list_tasks(project_id=proj_id)
    assert len(rows) == 1


def test_list_tasks_combines_status_and_assigned_to(workspace):
    """Combined filter is an AND — both predicates ride the WHERE."""
    proj_id = workspace["project_id"]
    # Seed a second task: also assigned to dev-1 but with a different status.
    now = "2026-05-23T00:00:00Z"
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "assigned_to, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (proj_id, "T2", "d", "complete", "dev-1", "atelier-pm-1", now, now),
    )
    conn.commit()
    conn.close()

    backend_local.assign_task(task_id=workspace["task_id"], agent_id="dev-1")

    assigned_dev1 = backend_local.list_tasks(
        project_id=proj_id, status="assigned", assigned_to="dev-1"
    )
    assert len(assigned_dev1) == 1
    assert assigned_dev1[0]["status"] == "assigned"
    assert assigned_dev1[0]["assigned_to"] == "dev-1"
