# tests/test_tasks.py
"""Tasks CRUD tests — exercise `scripts.tasks` through the backend facade.

The fixture stands up a fake workspace root (with a `.git` marker so
`backend_local._workspace_root()` resolves), applies both
`migrations/shared/` and `migrations/local-only/`, and seeds the
minimum v1.1.0 rows (workspaces / roles / agents / projects) directly
via sqlite3. We bypass the legacy `create_role` / `create_agent` /
`create_project` helpers because they still emit v1.0.13-shaped INSERTs
(no `workspace_id`, no `slug`) — Plan 3 Tasks 1-2 will catch those up.

`db_path` is passed to the task functions for signature parity, but the
backend facade resolves the active DB itself (`<workspace>/.ai/atelier.db`
in Local mode). Tests verify against the same path on disk.
"""

import sqlite3
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations
from scripts.tasks import (
    _coerce_priority,
    assign_task,
    claim_task,
    complete_task,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    search_tasks,
    update_task,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed(db_path: str) -> tuple[int, int, str]:
    """Seed workspace + role + agent + project. Returns (ws_id, proj_id, agent_id)."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("auth", "repo:auth", "Auth", "test workspace", now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("developer", "Writes code", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("dev-1", "Alice", role_id, "Expert", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth", "OAuth2", "design:open", "dev-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ws_id, proj_id, "dev-1"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Stand up a fake workspace at tmp_path/repo with a migrated atelier.db.

    Forces Local mode by monkey-patching `mode_detector.detect_mode` —
    we don't want the test suite to depend on whether `~/.memex/` is
    installed on the runner. The shared conftest already clears the
    `detect_mode` cache around each test so the patch can't leak.
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
    ws_id, proj_id, agent_id = _seed(str(db))
    return {"db_path": str(db), "agent_id": agent_id, "project_id": proj_id, "workspace_id": ws_id}


def test_create_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    task = create_task(
        db,
        project_id=project_id,
        title="Write failing auth tests",
        description="TDD red phase for JWT validation",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assert task["id"] == 1
    assert task["status"] == "pending"
    assert task["assigned_to"] is None


def test_create_task_coerces_string_priority(setup):
    """v1.0.13 callers passed 'critical'|'high'|'medium'|'low'; the
    coercion helper maps those to the v1.1.0 INTEGER column."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    task = create_task(
        db,
        project_id=project_id,
        title="urgent",
        created_by=agent_id,
        priority="critical",
        workspace_id=setup["workspace_id"],
    )
    assert task["priority"] == 4


def test_get_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    task = get_task(db, 1)
    assert task["title"] == "Write tests"


def test_get_task_missing_returns_none(setup):
    assert get_task(setup["db_path"], 999) is None


def test_assign_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    task = assign_task(db, task_id=1, agent_id=agent_id)
    assert task["assigned_to"] == agent_id
    assert task["status"] == "assigned"


def test_claim_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assign_task(db, task_id=1, agent_id=agent_id)
    task = claim_task(db, task_id=1, agent_id=agent_id)
    assert task["status"] == "in-progress"


def test_complete_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assign_task(db, task_id=1, agent_id=agent_id)
    claim_task(db, task_id=1, agent_id=agent_id)
    task = complete_task(db, task_id=1)
    assert task["status"] == "complete"


def test_update_task_notes(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    task = update_task(db, 1, notes="Blocked on missing mock library")
    assert task["notes"] == "Blocked on missing mock library"


def test_delete_task(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write tests",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assert delete_task(db, 1) is True
    assert get_task(db, 1) is None


def test_list_tasks_by_status(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Task A",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    create_task(
        db,
        project_id=project_id,
        title="Task B",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assign_task(db, task_id=2, agent_id=agent_id)
    pending = list_tasks(db, status="pending", project_id=project_id)
    assert len(pending) == 1
    assert pending[0]["title"] == "Task A"


def test_search_tasks(setup):
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Write JWT tests",
        description="Test token validation",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    create_task(
        db,
        project_id=project_id,
        title="Fix login bug",
        description="Auth redirect broken",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    results = search_tasks(db, query="JWT")
    assert len(results) == 1
    assert results[0]["title"] == "Write JWT tests"


# ── _coerce_priority unit tests ────────────────────────────────────────────


def test_coerce_priority_known_strings():
    assert _coerce_priority("critical") == 4
    assert _coerce_priority("high") == 3
    assert _coerce_priority("medium") == 2
    assert _coerce_priority("low") == 1


def test_coerce_priority_case_insensitive():
    assert _coerce_priority("CRITICAL") == 4
    assert _coerce_priority("High") == 3


def test_coerce_priority_unknown_string_returns_zero():
    assert _coerce_priority("nonsense") == 0
    assert _coerce_priority("") == 0


def test_coerce_priority_int_passthrough():
    assert _coerce_priority(0) == 0
    assert _coerce_priority(3) == 3
    assert _coerce_priority(99) == 99


def test_coerce_priority_none_returns_zero():
    assert _coerce_priority(None) == 0


# ──────────────────────────────────────────────────────────────────────────
# atelier#33 — cross-project list_tasks in Memex mode
# ──────────────────────────────────────────────────────────────────────────


def test_list_tasks_local_mode_cross_project_returns_all(setup):
    """Local mode + project_id=None: full-table scan covers tasks from
    ALL projects (existing behavior — verifying it still works after
    the Memex-mode branch was added)."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    workspace_id = setup["workspace_id"]
    # Seed a second project via raw SQL (mirrors _seed pattern) so we can
    # write tasks to both projects without colliding on slugs.
    import sqlite3 as _s

    conn = _s.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, phase, description, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            "second-proj",
            "Second project",
            "design:open",
            "",
            agent_id,
            "2026-05-18T00:00:00Z",
            "2026-05-18T00:00:00Z",
        ),
    )
    conn.commit()
    second_pid = conn.execute("SELECT id FROM projects WHERE slug = 'second-proj'").fetchone()[0]
    conn.close()
    create_task(
        db,
        project_id=project_id,
        title="Task in P1",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    create_task(
        db,
        project_id=second_pid,
        title="Task in P2",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    all_tasks = list_tasks(db, project_id=None)
    titles = sorted(t["title"] for t in all_tasks)
    assert titles == ["Task in P1", "Task in P2"]


def test_list_tasks_memex_mode_cross_project_calls_backend(monkeypatch):
    """Memex mode + project_id=None: routes to
    backend_memex.list_tasks_cross_project (no NotImplementedError)."""
    from scripts import backend_memex, mode_detector, tasks

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    seen = {}

    def fake_cross_project(*, status=None, assigned_to=None):
        seen["status"] = status
        seen["assigned_to"] = assigned_to
        return [{"id": 1, "title": "from-memex"}]

    monkeypatch.setattr(backend_memex, "list_tasks_cross_project", fake_cross_project)
    result = tasks.list_tasks("/unused", project_id=None, status="pending")
    assert seen == {"status": "pending", "assigned_to": None}
    assert result == [{"id": 1, "title": "from-memex"}]


def test_list_tasks_memex_mode_with_project_id_uses_backend_list_tasks(monkeypatch):
    """Memex mode + project_id set: still routes through backend.list_tasks
    (project-scoped facade, spec §4.3) — cross-project surface is NOT
    used when caller supplies a project_id."""
    from scripts import backend, mode_detector, tasks

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    seen = {}

    def fake_list_tasks(*, project_id, status=None, assigned_to=None):
        seen["project_id"] = project_id
        return [{"id": 7, "project_id": project_id}]

    monkeypatch.setattr(backend, "list_tasks", fake_list_tasks)
    result = tasks.list_tasks("/unused", project_id=42)
    assert seen == {"project_id": 42}
    assert result == [{"id": 7, "project_id": 42}]


# ──────────────────────────────────────────────────────────────────────────
# atelier#34 — tasks.parallel_group reintroduction (migration 004)
# ──────────────────────────────────────────────────────────────────────────


def test_create_task_default_parallel_group_is_null(setup):
    """Back-compat: callers that don't pass parallel_group get NULL.
    Existing tests that pre-date #34 must keep passing — the column
    is additive and nullable."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    task = create_task(
        db,
        project_id=project_id,
        title="No parallel group set",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    assert task["parallel_group"] is None


def test_create_task_with_explicit_parallel_group(setup):
    """When parallel_group is provided, it lands on the row and survives
    the write/round-trip-read cycle. Consumed by atelier#39's planner +
    dispatch.py wave grouping."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    task = create_task(
        db,
        project_id=project_id,
        title="In parallel group 3",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
        parallel_group=3,
    )
    assert task["parallel_group"] == 3


def test_update_task_can_set_parallel_group(setup):
    """`backend.update_task` accepts parallel_group via the
    _UPDATE_TASK_ALLOWED_COLUMNS allowlist; the per-column UPDATE branch
    in backend_local lands it on the existing row."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Assign me a parallel group later",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    task = update_task(db, 1, parallel_group=7)
    assert task["parallel_group"] == 7


def test_update_task_can_clear_parallel_group_back_to_null(setup):
    """Setting parallel_group=None on an existing row must actually NULL
    the column — operators need a way to remove a tag, not just change it."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="Will be cleared",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
        parallel_group=5,
    )
    cleared = update_task(db, 1, parallel_group=None)
    assert cleared["parallel_group"] is None


def test_update_task_rejects_status_alongside_parallel_group(setup):
    """Defense-in-depth: status still routes through update_task_status
    even when paired with parallel_group. Facade rejects with the
    canonical message; mixing the two must not silently allow status."""
    db, agent_id, project_id = setup["db_path"], setup["agent_id"], setup["project_id"]
    create_task(
        db,
        project_id=project_id,
        title="t",
        created_by=agent_id,
        workspace_id=setup["workspace_id"],
    )
    # scripts.tasks.update_task splits status from other columns and
    # routes status through update_task_status; parallel_group lands via
    # the regular update path. Both succeed.
    task = update_task(db, 1, parallel_group=2, status="assigned", assigned_to=agent_id)
    assert task["parallel_group"] == 2
    assert task["status"] == "assigned"
