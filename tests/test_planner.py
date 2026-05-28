"""Planner tests — scripts/planner.py (atelier#58).

Real-substrate contract tests: the planner is gated by the REAL
``scripts.dag.validate_dag`` and persists through the REAL
``scripts.tasks.create_task`` facade against a migrated Local-mode
``.ai/atelier.db`` (no invented mocks of the gate or the DB — A2). The only
injected seam is the ``synthesize`` callable, which stands in for the wave-1
agent dispatch (Python cannot spawn agents).

Coverage (#58 acceptance):
  * planner produces a valid DAG → tasks persisted with ``parallel_group`` set;
  * planner-invalid DAGs raise during synthesis (PlannerEscalation), NOT at PM
    dispatch — and persist nothing;
  * planner re-synthesizes on a single DagValidationError before giving up
    (dag-invalid → exactly one retry → escalate);
  * synthesis-failure escalates with ZERO retries;
  * null ``parallel_group`` is rejected by the planner's own gate (validate_dag
    tolerates it);
  * a dag-invalid first attempt that is FIXED on the retry succeeds;
  * persist is all-or-nothing (mid-loop failure leaves zero rows).
"""

import sqlite3
from pathlib import Path

import pytest

from scripts import planner
from scripts import tasks as tasks_mod
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed(db_path: str) -> tuple[int, int, str]:
    now = "2026-05-28T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("auth", "repo:auth", "Auth", "test workspace", now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("developer", "Writes code", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dev-1", "Alice", role_id, "Expert", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, phase, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth", "OAuth2", "plan:open", "dev-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ws_id, proj_id, "dev-1"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Local-mode migrated atelier.db + seeded project (mirrors test_tasks.py)."""
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


def _valid_tasks() -> list[dict]:
    """A 3-task, 2-wave DAG that passes validate_dag with no pre-existing files:
    t-1 (wave 1) writes a.py; t-2 (wave 1) writes b.py; t-3 (wave 2) depends on
    t-1+t-2, reads a.py+b.py, writes c.py."""
    return [
        {
            "task_id": "t-1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["a.py"],
            "description": "build a",
        },
        {
            "task_id": "t-2",
            "assigned_persona": "sdet-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["b.py"],
            "description": "build b",
        },
        {
            "task_id": "t-3",
            "assigned_persona": "software-architect-1",
            "parallel_group": 2,
            "depends_on": ["t-1", "t-2"],
            "reads": ["a.py", "b.py"],
            "writes": ["c.py"],
            "description": "combine",
        },
    ]


def _fenced(tasks: list[dict]) -> str:
    import json

    return "Here is the plan:\n\n```json\n" + json.dumps(tasks) + "\n```\n"


def _synth_from(*responses: str):
    """Build a synthesize(error=None) callable returning `responses` in order;
    records how many times it was called."""
    state = {"calls": 0}

    def synthesize(error=None):
        i = state["calls"]
        state["calls"] += 1
        return responses[min(i, len(responses) - 1)]

    synthesize.state = state  # type: ignore[attr-defined]
    return synthesize


# ── parse_task_list ───────────────────────────────────────────────────────


def test_parse_task_list_accepts_fenced_json():
    tasks = planner.parse_task_list(_fenced(_valid_tasks()))
    assert [t["task_id"] for t in tasks] == ["t-1", "t-2", "t-3"]


@pytest.mark.parametrize("raw", ["", "   ", "not json", "{}", "[]", "[1, 2]"])
def test_parse_task_list_rejects_non_list_as_synthesis_failure(raw):
    with pytest.raises(planner.PlannerSynthesisFailure):
        planner.parse_task_list(raw)


# ── validate_tasks own-gate ───────────────────────────────────────────────


def test_validate_tasks_rejects_null_parallel_group():
    tasks = _valid_tasks()
    tasks[0]["parallel_group"] = None  # validate_dag TOLERATES this; planner must not
    with pytest.raises(planner.PlannerDagInvalid, match="parallel_group"):
        planner.validate_tasks(tasks, existing_files=set())


def test_validate_tasks_passes_clean_dag():
    planner.validate_tasks(_valid_tasks(), existing_files=set())  # no raise


# ── run_planner happy path ────────────────────────────────────────────────


def test_run_planner_persists_valid_dag_with_parallel_group(setup):
    synth = _synth_from(_fenced(_valid_tasks()))
    ids = planner.run_planner(
        synthesize=synth,
        db_path=setup["db_path"],
        project_id=setup["project_id"],
        created_by=setup["agent_id"],
        existing_files=set(),
        workspace_id=setup["workspace_id"],
    )
    assert len(ids) == 3
    assert synth.state["calls"] == 1  # no retry on the happy path
    # Persisted rows carry the wave (assert via get_task, not the return value).
    waves = sorted(tasks_mod.get_task(setup["db_path"], tid)["parallel_group"] for tid in ids)
    assert waves == [1, 1, 2]
    for tid in ids:
        assert tasks_mod.get_task(setup["db_path"], tid)["parallel_group"] is not None


# ── dag-invalid → exactly one retry → escalate ────────────────────────────


def test_run_planner_dag_invalid_retries_once_then_escalates(setup):
    # Orphan dep: t-3 depends on a task_id not in the list → OrphanDepsError.
    bad = _valid_tasks()
    bad[2]["depends_on"] = ["t-1", "t-99"]
    synth = _synth_from(_fenced(bad))  # same bad list on every (re)prompt
    with pytest.raises(planner.PlannerEscalation) as exc:
        planner.run_planner(
            synthesize=synth,
            db_path=setup["db_path"],
            project_id=setup["project_id"],
            created_by=setup["agent_id"],
            existing_files=set(),
            workspace_id=setup["workspace_id"],
        )
    assert exc.value.kind == "dag-invalid"
    assert exc.value.attempts == 2  # one initial + exactly one retry
    assert synth.state["calls"] == 2
    # Raised during synthesis, NOT at PM dispatch — nothing persisted.
    assert tasks_mod.list_tasks(setup["db_path"], project_id=setup["project_id"]) == []


def test_run_planner_dag_invalid_fixed_on_retry_succeeds(setup):
    bad = _valid_tasks()
    bad[2]["depends_on"] = ["t-1", "t-99"]  # orphan on first attempt
    synth = _synth_from(_fenced(bad), _fenced(_valid_tasks()))  # fixed on retry
    ids = planner.run_planner(
        synthesize=synth,
        db_path=setup["db_path"],
        project_id=setup["project_id"],
        created_by=setup["agent_id"],
        existing_files=set(),
        workspace_id=setup["workspace_id"],
    )
    assert len(ids) == 3
    assert synth.state["calls"] == 2  # took the one retry, then succeeded


# ── synthesis-failure → escalate, ZERO retries ────────────────────────────


@pytest.mark.parametrize("raw", ["", "garbage not json", "[]"])
def test_run_planner_synthesis_failure_escalates_with_zero_retries(setup, raw):
    synth = _synth_from(raw)
    with pytest.raises(planner.PlannerEscalation) as exc:
        planner.run_planner(
            synthesize=synth,
            db_path=setup["db_path"],
            project_id=setup["project_id"],
            created_by=setup["agent_id"],
            existing_files=set(),
            workspace_id=setup["workspace_id"],
        )
    assert exc.value.kind == "synthesis-failure"
    assert exc.value.attempts == 1
    assert synth.state["calls"] == 1  # NO retry — nothing to correct
    assert tasks_mod.list_tasks(setup["db_path"], project_id=setup["project_id"]) == []


# ── persist atomicity ─────────────────────────────────────────────────────


def test_persist_tasks_is_all_or_nothing(setup, monkeypatch):
    """A mid-loop create_task failure rolls back the rows already created."""
    real_create = tasks_mod.create_task
    calls = {"n": 0}

    def flaky_create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated backend failure on the 2nd row")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(planner.tasks_mod, "create_task", flaky_create)
    with pytest.raises(RuntimeError, match="simulated backend failure"):
        planner.persist_tasks(
            _valid_tasks(),
            db_path=setup["db_path"],
            project_id=setup["project_id"],
            created_by=setup["agent_id"],
            workspace_id=setup["workspace_id"],
        )
    # The first row was created then rolled back → zero residual rows.
    assert tasks_mod.list_tasks(setup["db_path"], project_id=setup["project_id"]) == []


# ── reviewer disjointness (atelier#59) ────────────────────────────────────


def _impl_review_pair(
    reviewer_persona: str = "code-reviewer-1", reviews: str = "t-1"
) -> list[dict]:
    """A valid 2-wave DAG: t-1 implements (backend-engineer-1, writes a.py);
    t-2 reviews t-1 (reviewer_persona, depends_on+reads t-1's output). Defaults
    are disjoint (code-reviewer-1 != backend-engineer-1) and dag-clean."""
    return [
        {
            "task_id": "t-1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["a.py"],
            "description": "implement foo",
        },
        {
            "task_id": "t-2",
            "assigned_persona": reviewer_persona,
            "parallel_group": 2,
            "depends_on": ["t-1"],
            "reviews": reviews,
            "reads": ["a.py"],
            "writes": [],
            "description": "review foo",
        },
    ]


def test_check_reviewer_disjointness_passes_disjoint():
    planner.check_reviewer_disjointness(_impl_review_pair())  # no raise


def test_check_reviewer_disjointness_rejects_same_persona():
    bad = _impl_review_pair(reviewer_persona="backend-engineer-1")  # == implementer
    with pytest.raises(planner.PlannerDagInvalid) as exc:
        planner.check_reviewer_disjointness(bad)
    msg = str(exc.value)
    # The message must name reviewer task_id, the shared persona, and the reviewed task_id.
    assert "reviewer-disjointness" in msg
    assert "t-2" in msg and "t-1" in msg and "backend-engineer-1" in msg


def test_check_reviewer_disjointness_rejects_orphan_reviews():
    bad = _impl_review_pair(reviews="t-99")  # not in list
    with pytest.raises(planner.PlannerDagInvalid, match="orphan-reviews"):
        planner.check_reviewer_disjointness(bad)


def test_check_reviewer_disjointness_rejects_self_review():
    tasks = _impl_review_pair()
    tasks[1]["reviews"] = "t-2"  # reviews itself
    with pytest.raises(planner.PlannerDagInvalid, match="self-review"):
        planner.check_reviewer_disjointness(tasks)


def test_check_reviewer_disjointness_rejects_missing_persona():
    """A null/absent persona on either side is a DEFECT, never 'disjoint by absence'."""
    tasks = _impl_review_pair()
    del tasks[0]["assigned_persona"]  # reviewed task has no persona
    with pytest.raises(planner.PlannerDagInvalid, match="reviewer-disjointness"):
        planner.check_reviewer_disjointness(tasks)


def test_check_reviewer_disjointness_rejects_non_string_reviews():
    tasks = _impl_review_pair()
    tasks[1]["reviews"] = ["t-1"]  # list, not a single string
    with pytest.raises(planner.PlannerDagInvalid, match="reviewer-disjointness"):
        planner.check_reviewer_disjointness(tasks)


def test_check_reviewer_disjointness_no_false_positive_shared_persona():
    """Two IMPLEMENT tasks may share a persona — only a review task vs the task
    it reviews is constrained."""
    planner.check_reviewer_disjointness(
        [
            {"task_id": "a", "assigned_persona": "be-1", "parallel_group": 1},
            {"task_id": "b", "assigned_persona": "be-1", "parallel_group": 1},
        ]
    )  # no raise


def test_check_reviewer_disjointness_absent_reviews_is_non_review():
    planner.check_reviewer_disjointness(_valid_tasks())  # no `reviews` anywhere → no raise


def test_run_planner_rejects_reviewer_violation_at_validate_not_dispatch(setup):
    """A same-persona reviewer is rejected during synthesis/validate (one retry
    then escalate), NOT at dispatch — and nothing is persisted."""
    bad = _impl_review_pair(reviewer_persona="backend-engineer-1")
    synth = _synth_from(_fenced(bad))  # same bad list on both attempts
    with pytest.raises(planner.PlannerEscalation) as exc:
        planner.run_planner(
            synthesize=synth,
            db_path=setup["db_path"],
            project_id=setup["project_id"],
            created_by=setup["agent_id"],
            existing_files=set(),
            workspace_id=setup["workspace_id"],
        )
    assert exc.value.kind == "dag-invalid"
    assert exc.value.attempts == 2  # one initial + exactly one retry
    assert synth.state["calls"] == 2
    assert tasks_mod.list_tasks(setup["db_path"], project_id=setup["project_id"]) == []


def test_run_planner_persists_valid_disjoint_list(setup):
    """A valid disjoint list passes the in-pipeline gate (persists) AND the
    standalone check (the #60 dispatch-time arm) returns None on the same list."""
    good = _impl_review_pair()
    synth = _synth_from(_fenced(good))
    ids = planner.run_planner(
        synthesize=synth,
        db_path=setup["db_path"],
        project_id=setup["project_id"],
        created_by=setup["agent_id"],
        existing_files=set(),
        workspace_id=setup["workspace_id"],
    )
    assert len(ids) == 2
    assert planner.check_reviewer_disjointness(good) is None  # standalone arm agrees
    # `reviews` is validation-time-only — never reaches the persisted row.
    for tid in ids:
        assert "reviews" not in tasks_mod.get_task(setup["db_path"], tid)
