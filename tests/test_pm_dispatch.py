"""AI-5 — pytest suite for `scripts/pm_dispatch.py` (atelier#60, the wave-5
PM dispatch loop).

`WaveDispatcher.run` is mode-agnostic: it reaches the outside world only through
three injected seams (`spawn_fn` / `poll_fn` / `escalate_fn`) plus an injectable
`clock` and `sleep_fn`. Durable state (attempts / abandon) is mutated through
`scripts.tasks` → `backend_local`, so these tests stand up a real Local-mode
SQLite DB with all migrations applied (mirroring `tests/test_backend_local_state.py`),
seed task rows, and inject deterministic fakes — no real subprocesses, no real
sleeps, no real wall-clock.

Matrix covered here:

* ATOMIC NULL-parallel_group preflight: a NULL group in ANY wave raises
  NullParallelGroupError (a DispatchError subclass) and spawns ZERO workers.
* 3-wave happy path completes waves in order.
* The wave barrier: wave-2 does NOT start until wave-1 is fully terminal-only;
  BOTH `blocked` AND `needs-input` HOLD the barrier (re-dispatched, never
  release it as a non-terminal status).
* PM-side wall-clock via an INJECTED advanceable clock: advancing past
  WALL_CLOCK_S soft-kills the attempt and charges it (no real sleeps).
* Attempt-budget exhaustion (5 attempts) → set_abandoned(category='capacity')
  AND escalate_fn called.
* Abandon ALWAYS emits an escalation (same code path).
* Wave advances while abandoned_ack_at IS NULL (ack is non-gating).
* Cascade-abandon: a dependent of an abandoned task gets abandon_category
  'blocked', an escalation, NO attempt charge; a cyclic dep-graph terminates.
* Determinism: identical task fixtures → identical wave partition across calls.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.dispatch import DispatchError
from scripts.migrate import apply_migrations
from scripts.pm_dispatch import (
    MAX_ATTEMPTS,
    MAX_PARALLEL_WORKERS,
    WALL_CLOCK_S,
    NullParallelGroupError,
    WaveDispatcher,
    partition_waves,
    preflight_validate,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── Local-mode DB fixture (mirrors test_backend_local_state.py) ─────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace.

    `backend_local._conn()` resolves the DB via the CWD git root, so we chdir
    into the workspace and drop a `.git` dir. `detect_mode` is forced to
    'local' so the `tasks.*` mutators route to `backend_local` (no Memex)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    now = "2026-05-29T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("proj", "repo:proj", "Proj", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "p", "P", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db), "project_id": proj_id}


def _seed_task(
    workspace,
    *,
    title,
    parallel_group,
    created_at="2026-05-29T00:00:00Z",
    status="pending",
):
    """Insert a task row directly (so we control parallel_group / created_at /
    status) and return its id."""
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "parallel_group, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace["project_id"],
            title,
            "d",
            status,
            parallel_group,
            "atelier-pm-1",
            created_at,
            created_at,
        ),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _task_row(workspace, task_id):
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row)


# ── Fake seams ──────────────────────────────────────────────────────────────


class FakeClock:
    """A monotonic, manually-advanceable clock. `advance` is the ONLY way time
    moves — no real sleeps, no argless now()."""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def _done_envelope(task_id, attempt):
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": "done",
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "done",
        "next_action": "review",
    }


def _status_envelope(task_id, attempt, status):
    """A valid envelope for any closure status. Empty artifacts is legal for
    blocked/needs-input; non-empty otherwise."""
    artifacts = [] if status in ("blocked", "needs-input") else [{"path": "f.py", "sha": "s"}]
    notes = "ABANDON: scope:out of scope" if status == "abandoned" else "notes"
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "artifacts": artifacts,
        "notes_md": notes,
        "next_action": "none",
    }


# ── Pure helpers: preflight + partition ─────────────────────────────────────


def test_preflight_rejects_null_parallel_group_atomically():
    """A NULL parallel_group on a non-terminal task raises
    NullParallelGroupError naming the offender."""
    tasks = [
        {"id": 1, "parallel_group": 0, "status": "pending"},
        {"id": 2, "parallel_group": None, "status": "pending"},
    ]
    with pytest.raises(NullParallelGroupError) as exc:
        preflight_validate(tasks)
    assert 2 in exc.value.task_ids
    # It is a DispatchError subclass (operator-facing fail-loud).
    assert isinstance(exc.value, DispatchError)


def test_preflight_ignores_null_group_on_terminal_task():
    """A NULL group on an already-terminal task is NOT an offender — it needs
    no dispatch, so it has no wave."""
    tasks = [
        {"id": 1, "parallel_group": 0, "status": "pending"},
        {"id": 2, "parallel_group": None, "status": "complete"},
        {"id": 3, "parallel_group": None, "status": "abandoned"},
    ]
    preflight_validate(tasks)  # no raise


def test_partition_waves_is_deterministic():
    """Identical task fixtures → identical wave partition across two calls
    (the (parallel_group, created_at, id) sort is total)."""
    tasks = [
        {"id": 3, "parallel_group": 1, "created_at": "t", "status": "pending"},
        {"id": 1, "parallel_group": 0, "created_at": "t", "status": "pending"},
        {"id": 2, "parallel_group": 0, "created_at": "t", "status": "pending"},
        {"id": 4, "parallel_group": 1, "created_at": "t", "status": "pending"},
    ]
    w1 = partition_waves(tasks)
    w2 = partition_waves(tasks)
    ids = [[t["id"] for t in wave] for wave in w1]
    assert ids == [[1, 2], [3, 4]]
    assert [[t["id"] for t in wave] for wave in w2] == ids


def test_partition_waves_excludes_terminal_tasks():
    tasks = [
        {"id": 1, "parallel_group": 0, "created_at": "t", "status": "complete"},
        {"id": 2, "parallel_group": 0, "created_at": "t", "status": "pending"},
    ]
    waves = partition_waves(tasks)
    assert [[t["id"] for t in w] for w in waves] == [[2]]


# ── Engine: NULL preflight spawns ZERO workers ──────────────────────────────


def test_run_null_group_preflight_spawns_nothing(workspace):
    """A NULL group in ANY wave → run raises BEFORE any spawn. spawn_fn is
    never called (the whole batch is rejected atomically)."""
    spawned = []
    poll_calls = []
    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawned.append(task["id"]),
        poll_fn=lambda task, attempt: poll_calls.append(task["id"]) or None,
        clock=FakeClock(),
    )
    tasks = [
        {"id": 1, "parallel_group": 0, "status": "pending", "created_at": "t"},
        {"id": 2, "parallel_group": None, "status": "pending", "created_at": "t"},
    ]
    with pytest.raises(NullParallelGroupError):
        d.run(tasks)
    assert spawned == []
    assert poll_calls == []


# ── Engine: 3-wave happy path in order ──────────────────────────────────────


def test_three_wave_happy_path_completes_in_order(workspace):
    """Three waves, one task each, all report `done` on first poll. The waves
    are dispatched + closed in parallel_group order."""
    t0 = _seed_task(workspace, title="w0", parallel_group=0)
    t1 = _seed_task(workspace, title="w1", parallel_group=1)
    t2 = _seed_task(workspace, title="w2", parallel_group=2)

    spawn_order = []

    def spawn_fn(task, attempt):
        spawn_order.append(task["id"])

    def poll_fn(task, attempt):
        return _done_envelope(task["id"], attempt)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=spawn_fn,
        poll_fn=poll_fn,
        clock=FakeClock(),
    )
    tasks = [_task_row(workspace, t) for t in (t2, t0, t1)]  # unordered input
    summaries = d.run(tasks)

    # Three wave summaries, each fully terminal-only.
    assert len(summaries) == 3
    assert all(s["terminal_only"] for s in summaries)
    # Dispatched in wave order despite shuffled input.
    assert spawn_order == [t0, t1, t2]
    # Each `done` is persisted durably (status -> 'complete'), symmetric with
    # the abandon path, so a resume would not re-dispatch it. attempts charged
    # exactly once per task.
    for tid in (t0, t1, t2):
        row = _task_row(workspace, tid)
        assert row["attempts"] == 1
        assert row["status"] == "complete"


# ── Engine: MAX_PARALLEL_WORKERS cap bounds in-flight concurrency ───────────


def test_single_wave_respects_max_parallel_workers_cap(workspace):
    """8 tasks in ONE parallel_group (a single wave) are dispatched with at most
    MAX_PARALLEL_WORKERS in flight at any instant, yet all 8 still reach done.

    We track the live in-flight set ourselves: spawn_fn adds the task (and
    records the running max), and a terminal poll removes it. Each worker stays
    in-flight for one extra poll round (returns None once, then done) so the
    engine genuinely wants to top up the in-flight set on every round — if the
    `len(in_flight) < MAX_PARALLEL_WORKERS` guard were removed, all 8 would be
    spawned at once and `max_concurrent` would hit 8, failing the assertion.

    The constant is IMPORTED, not hardcoded — this binds to the engine's cap."""
    tids = [_seed_task(workspace, title=f"t{i}", parallel_group=0) for i in range(8)]

    in_flight = set()
    max_concurrent = 0
    poll_counts = {}

    def spawn_fn(task, attempt):
        nonlocal max_concurrent
        in_flight.add(task["id"])
        max_concurrent = max(max_concurrent, len(in_flight))

    def poll_fn(task, attempt):
        # First poll: keep the worker in flight (None). Second poll: terminal.
        poll_counts[task["id"]] = poll_counts.get(task["id"], 0) + 1
        if poll_counts[task["id"]] < 2:
            return None
        in_flight.discard(task["id"])
        return _done_envelope(task["id"], attempt)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=spawn_fn,
        poll_fn=poll_fn,
        clock=FakeClock(),
        sleep_fn=lambda s: None,
    )
    tasks = [_task_row(workspace, t) for t in tids]
    summaries = d.run(tasks)

    # The cap was never exceeded …
    assert max_concurrent <= MAX_PARALLEL_WORKERS
    # … and 8 tasks in one wave means concurrency was actually pressured (the
    # test would be vacuous if the wave fit under the cap).
    assert len(tids) > MAX_PARALLEL_WORKERS
    # … yet every task still closed `done` in the single wave.
    assert len(summaries) == 1
    assert summaries[0]["terminal_only"] is True
    for t in tids:
        assert _task_row(workspace, t)["status"] == "complete"


# ── Engine: the barrier holds on blocked AND needs-input ────────────────────


@pytest.mark.parametrize("holding_status", ["blocked", "needs-input"])
def test_barrier_holds_until_wave_terminal_only(workspace, holding_status):
    """Wave-2 must NOT start until wave-1 is fully terminal-only. A
    blocked/needs-input reply HOLDS the barrier: the engine re-dispatches the
    wave-1 task and never touches wave-2 while wave-1 is non-terminal. Here the
    wave-1 task holds the barrier on attempts 1..(N-1), then converges to done;
    we assert NO wave-2 spawn happened before wave-1 closed."""
    t_w1 = _seed_task(workspace, title="w1", parallel_group=0)
    t_w2 = _seed_task(workspace, title="w2", parallel_group=1)

    spawn_log = []  # (task_id, "spawn")
    # Per-task poll counter so the wave-1 task reports holding status twice,
    # then done; the wave-2 task always reports done.
    counts = {t_w1: 0, t_w2: 0}

    def spawn_fn(task, attempt):
        spawn_log.append(task["id"])

    def poll_fn(task, attempt):
        counts[task["id"]] += 1
        if task["id"] == t_w1 and counts[task["id"]] < 3:
            return _status_envelope(task["id"], attempt, holding_status)
        return _done_envelope(task["id"], attempt)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=spawn_fn,
        poll_fn=poll_fn,
        clock=FakeClock(),
    )
    tasks = [_task_row(workspace, t_w1), _task_row(workspace, t_w2)]
    summaries = d.run(tasks)

    # The wave-2 task was NOT spawned until the wave-1 task closed terminal.
    first_w2_index = spawn_log.index(t_w2)
    # Every spawn before the first wave-2 spawn must be the wave-1 task.
    assert all(tid == t_w1 for tid in spawn_log[:first_w2_index])
    # Wave-1 was re-dispatched (held the barrier) before closing → >1 spawn.
    assert spawn_log[:first_w2_index].count(t_w1) == 3
    assert len(summaries) == 2
    assert all(s["terminal_only"] for s in summaries)


# ── Engine: PM-side wall-clock soft-kill via injected clock ─────────────────


def test_wall_clock_soft_kill_charges_attempt(workspace):
    """A silently-dead worker (poll always None) is soft-killed once the
    INJECTED clock advances past WALL_CLOCK_S. The soft-killed dispatch counts
    as an attempt. No real sleeps: the fake clock jumps the wall-clock each
    poll round."""
    tid = _seed_task(workspace, title="silent", parallel_group=0)

    clock = FakeClock()

    def poll_fn(task, attempt):
        # Worker never reports; advance time past the cap so the NEXT clock()
        # read in the poll loop trips the soft-kill.
        clock.advance(WALL_CLOCK_S + 1.0)
        return None

    spawns = []
    escalations = []
    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawns.append((task["id"], attempt)),
        poll_fn=poll_fn,
        escalate_fn=lambda e: escalations.append(e),
        clock=clock,
        sleep_fn=lambda s: None,  # never actually sleep
    )
    summaries = d.run([_task_row(workspace, tid)])

    # The task burned all 5 attempts (each soft-killed), then abandoned.
    row = _task_row(workspace, tid)
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["status"] == "abandoned"
    # Each dispatch was a distinct attempt 1..5.
    assert [a for (_, a) in spawns] == [1, 2, 3, 4, 5]
    assert summaries[-1]["terminal_only"] is True


def test_single_soft_kill_charges_exactly_one_attempt(workspace):
    """A SINGLE wall-clock soft-kill increments `attempts` by exactly 1, never
    2 (proving the soft-kill does not double-charge on top of the dispatch
    charge). Attempt 1 is soft-killed; attempt 2 reports done. The DB lands at
    attempts==2 — i.e. one charge per dispatch, the soft-killed dispatch being
    attempt 1 and NOT an extra charge."""
    tid = _seed_task(workspace, title="one-soft-kill", parallel_group=0)

    clock = FakeClock()
    spawns = []

    def poll_fn(task, attempt):
        if attempt == 1:
            # First dispatch goes silent — trip the soft-kill exactly once.
            clock.advance(WALL_CLOCK_S + 1.0)
            return None
        # Re-dispatch (attempt 2) reports done.
        return _done_envelope(task["id"], attempt)

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawns.append(attempt),
        poll_fn=poll_fn,
        clock=clock,
        sleep_fn=lambda s: None,
    )
    d.run([_task_row(workspace, tid)])

    row = _task_row(workspace, tid)
    # Exactly two dispatches → attempts charged exactly twice (1 then 2). The
    # single soft-kill added ONE charge, not two; otherwise this would be 3+.
    assert spawns == [1, 2]
    assert row["attempts"] == 2
    assert row["status"] == "complete"


# ── Engine: attempt-budget exhaustion → capacity abandon + escalation ───────


def test_budget_exhaustion_abandons_capacity_and_escalates(workspace):
    """A task that reports a non-terminal `blocked` on every attempt exhausts
    the 5-attempt budget → set_abandoned(category='capacity') AND escalate_fn
    is called (escalation emitted)."""
    tid = _seed_task(workspace, title="stuck", parallel_group=0)

    escalations = []
    spawns = []

    def poll_fn(task, attempt):
        return _status_envelope(task["id"], attempt, "blocked")

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawns.append(attempt),
        poll_fn=poll_fn,
        escalate_fn=lambda e: escalations.append(e),
        clock=FakeClock(),
        sleep_fn=lambda s: None,
    )
    d.run([_task_row(workspace, tid)])

    row = _task_row(workspace, tid)
    assert row["status"] == "abandoned"
    assert row["abandon_category"] == "capacity"
    assert row["attempts"] == MAX_ATTEMPTS
    # Exactly one escalation, on the same code path as the abandon.
    assert len(escalations) == 1
    assert escalations[0]["category"] == "capacity"
    assert escalations[0]["task_id"] == tid
    # The engine also records the escalation in its audit list.
    assert d.escalations == escalations


def test_worker_self_abandon_always_escalates(workspace):
    """A worker that returns an `abandoned` envelope is recorded with the
    parsed category AND escalated on the SAME code path (escalation is
    guaranteed, never best-effort)."""
    tid = _seed_task(workspace, title="giveup", parallel_group=0)

    escalations = []
    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _status_envelope(task["id"], attempt, "abandoned"),
        escalate_fn=lambda e: escalations.append(e),
        clock=FakeClock(),
    )
    d.run([_task_row(workspace, tid)])

    row = _task_row(workspace, tid)
    assert row["status"] == "abandoned"
    # notes_md was "ABANDON: scope:..." → parsed category 'scope'.
    assert row["abandon_category"] == "scope"
    assert len(escalations) == 1
    assert escalations[0]["category"] == "scope"
    # Self-abandon is charged exactly once (at dispatch), not double-charged.
    assert row["attempts"] == 1


# ── Engine: abandoned_ack_at is non-gating ──────────────────────────────────


def test_wave_advances_while_abandoned_ack_is_null(workspace):
    """An abandoned wave-1 task is wave-terminal the instant it is recorded;
    abandoned_ack_at stays NULL yet wave-2 still dispatches (ack is non-gating
    audit, never a barrier)."""
    t_w1 = _seed_task(workspace, title="ab", parallel_group=0)
    t_w2 = _seed_task(workspace, title="ok", parallel_group=1)

    def poll_fn(task, attempt):
        if task["id"] == t_w1:
            return _status_envelope(task["id"], attempt, "abandoned")
        return _done_envelope(task["id"], attempt)

    spawned = []
    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawned.append(task["id"]),
        poll_fn=poll_fn,
        clock=FakeClock(),
    )
    summaries = d.run([_task_row(workspace, t_w1), _task_row(workspace, t_w2)])

    # Wave-1 abandoned with ack NULL …
    row1 = _task_row(workspace, t_w1)
    assert row1["status"] == "abandoned"
    assert row1["abandoned_ack_at"] is None
    # … yet wave-2 still ran to completion.
    assert t_w2 in spawned
    assert len(summaries) == 2
    assert all(s["terminal_only"] for s in summaries)


# ── Engine: cascade-abandon ─────────────────────────────────────────────────


def test_cascade_abandon_blocks_dependent_without_charging_attempt(workspace):
    """A wave-2 task depending on an abandoned wave-1 task is cascade-abandoned:
    status abandoned + abandon_category 'blocked', an escalation naming the
    upstream, NO attempt charge, and NO spawn (dispatching it is pointless)."""
    t_up = _seed_task(workspace, title="upstream", parallel_group=0)
    t_dep = _seed_task(workspace, title="dependent", parallel_group=1)

    spawned = []
    escalations = []

    def poll_fn(task, attempt):
        # Only the upstream should ever be polled; it self-abandons.
        return _status_envelope(task["id"], attempt, "abandoned")

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: spawned.append(task["id"]),
        poll_fn=poll_fn,
        escalate_fn=lambda e: escalations.append(e),
        clock=FakeClock(),
    )
    up_row = _task_row(workspace, t_up)
    dep_row = _task_row(workspace, t_dep)
    dep_row["depends_on"] = [t_up]  # in-memory edge (planner does not persist)
    d.run([up_row, dep_row])

    dep = _task_row(workspace, t_dep)
    assert dep["status"] == "abandoned"
    assert dep["abandon_category"] == "blocked"
    # Cascade does NOT charge an attempt against the dependent.
    assert dep["attempts"] == 0
    # The dependent was never spawned.
    assert t_dep not in spawned
    # Two escalations: the upstream self-abandon + the cascade.
    cascade = [e for e in escalations if e["task_id"] == t_dep]
    assert len(cascade) == 1
    assert cascade[0]["upstream_task_id"] == t_up
    assert cascade[0]["category"] == "blocked"


def test_cascade_abandon_survives_cyclic_dep_graph(workspace):
    """A cyclic depends_on graph (malformed planner output) must not infinite-
    loop the ancestor walk — the visited-set bounds it. With no abandoned
    ancestor, both tasks run normally."""
    t_a = _seed_task(workspace, title="a", parallel_group=0)
    t_b = _seed_task(workspace, title="b", parallel_group=0)

    a_row = _task_row(workspace, t_a)
    b_row = _task_row(workspace, t_b)
    a_row["depends_on"] = [t_b]
    b_row["depends_on"] = [t_a]  # cycle a → b → a

    d = WaveDispatcher(
        workspace["db"],
        spawn_fn=lambda task, attempt: None,
        poll_fn=lambda task, attempt: _done_envelope(task["id"], attempt),
        clock=FakeClock(),
    )
    # Must terminate (no infinite loop) and close both as done.
    summaries = d.run([a_row, b_row])
    assert len(summaries) == 1
    assert summaries[0]["terminal_only"] is True
    # Neither was abandoned (no abandoned ancestor in the cycle).
    assert _task_row(workspace, t_a)["status"] != "abandoned"
    assert _task_row(workspace, t_b)["status"] != "abandoned"
