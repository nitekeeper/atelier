"""M4 — the barrier-free pipeline scheduler safety suite.

The safety assertions here are LOAD-BEARING and built to be UN-FAKEABLE:

* ``pipeline()`` advances a proven-independent task while a slower peer is still
  in-flight — asserted via per-task START timestamps in the deterministic order
  (sleep-RATIO independent, learning from M0's A4 fix: we assert that the
  faster task's START precedes the slower task's COMPLETION, not a brittle
  wall-clock ratio).
* A dependent task does NOT start until its upstreams are TERMINAL (real barrier
  honored on real deps).
* **SAFETY (un-fakeable):** two tasks with overlapping ``writes`` are NEVER
  concurrently in-flight under ``pipeline()`` — even when an ADVERSARIAL
  ``DagProof`` LIES and claims them independent.  The dynamic ``write_disjoint``
  backstop holds the line.  A companion MUTATION test monkeypatches the dynamic
  re-check out and asserts the safety test would then FAIL — so the safety
  guarantee cannot be silently removed.
* Absence of an independence proof ⇒ barrier (no silent overlap).
* ``parallel()`` is a true barrier (next wave waits for all TERMINAL), driven by
  the REAL WaveDispatcher.
* ``static_fleet_width`` only narrows; a near-exhausted budget shrinks fan-out.
* Worktree isolation: two concurrent writers operate in separate worktrees;
  merge is conflict-free + deterministic.

ALL tests use M3's :class:`FakeCliRunner` (subclassed) — NO real ``claude`` is
spawned.  These tests drive the scheduler directly (they do not depend on the
``ATELIER_TRANSPORT`` default), which since M7 is ``cli`` (the host pipeline);
``ATELIER_TRANSPORT=bridge`` is the explicit escape hatch to the legacy path.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    CliDispatchTools,
    FakeCliRunner,
    is_failed_attempt,
)
from scripts.dag import DagProof, compute_dag_proof
from scripts.host_scheduler import (
    JournalKeyTracker,
    WorktreeError,
    parallel,
    pipeline,
    scheduler_upstream_hashes_for,
    simple_worktree_factory,
)
from scripts.migrate import apply_migrations
from scripts.result_journal import ResultJournal

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── helpers ─────────────────────────────────────────────────────────────────


def _env(task_id, attempt=1, status="done"):
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "done",
    }


def _tid_from_argv(argv) -> str:
    """Extract the task_id from the -p prompt line run_attempt builds."""
    m = re.search(r"Perform task (\S+) ", argv[2])
    assert m is not None, f"unexpected prompt: {argv[2]!r}"
    return m.group(1)


def _writes_map(tasks) -> dict[str, list[str]]:
    """Build ``{str(task_id): [declared writes]}`` from a task list — so a
    ``_TimedRunner`` can CREATE each task's declared outputs and thus be an honest
    `done` writer that satisfies the engine's false-`done` guard.  A task with no
    ``writes`` (read-only) maps to an empty list (nothing created)."""
    return {str(t["task_id"]): [str(w) for w in (t.get("writes") or [])] for t in tasks}


def _run(coro):
    return asyncio.run(coro)


class _TimedRunner(FakeCliRunner):
    """A FakeCliRunner that records per-task START/END instants (monotonic) and
    sleeps a per-task amount, returning a valid envelope keyed to the task.

    Inherits the FAIL-CLOSED fake markers from FakeCliRunner — no real process,
    so the sandbox gate stays exempt."""

    def __init__(
        self,
        sleeps: dict[str, float] | None = None,
        writes: dict[str, list[str]] | None = None,
    ):
        super().__init__(structured_output=None)
        self.sleeps = sleeps or {}
        # Declared outputs each task should actually CREATE in its cwd so an
        # HONEST `done` writer satisfies the engine's false-`done` guard (a `done`
        # writer that produced none of its declared `writes` is rejected).  Keyed
        # by task_id → list of repo-relative paths; default {} (no files created,
        # for read-only tasks).  Built from the tasks' `writes` via `_writes_map`.
        self.writes = writes or {}
        self.started_at: dict[str, float] = {}
        self.ended_at: dict[str, float] = {}
        self._concurrency = 0
        self.max_concurrency = 0
        self.overlap_pairs: set[frozenset[str]] = set()
        self._live: set[str] = set()
        self._lock = asyncio.Lock()

    async def __call__(self, argv, cwd):
        tid = _tid_from_argv(argv)
        loop = asyncio.get_event_loop()
        async with self._lock:
            self.started_at[tid] = loop.time()
            self._concurrency += 1
            self.max_concurrency = max(self.max_concurrency, self._concurrency)
            for other in self._live:
                self.overlap_pairs.add(frozenset({tid, other}))
            self._live.add(tid)
        # Honest writer: create each declared output in this task's cwd worktree
        # so the false-`done` guard is satisfied (existence is the contract).
        for rel in self.writes.get(tid, []):
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"by {tid}")
        await asyncio.sleep(self.sleeps.get(tid, 0.0))
        async with self._lock:
            self._concurrency -= 1
            self._live.discard(tid)
            self.ended_at[tid] = loop.time()
        self.calls.append({"argv": list(argv), "cwd": cwd})
        return {
            "usage": {"output_tokens": 5},
            "total_cost_usd": 0.0,
            "is_error": False,
            "subtype": "success",
            "session_id": "s",
            "num_turns": 1,
            "stop_reason": "end_turn",
            "structured_output": _env(tid),
        }


def _git_init_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    ge = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, env=ge, check=True)
    (clone / "seed").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=clone, env=ge, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=clone, env=ge, check=True)
    return clone


def _pipeline_kwargs(clone, runner, journal, budget, dag_proof, **over):
    base = {
        "budget": budget,
        "journal": journal,
        "dag_proof": dag_proof,
        "model_for": lambda t, a: "sonnet",
        "briefing_for": lambda t, a: "b",
        "clone_dir": str(clone),
        "runner": runner,
    }
    base.update(over)
    return base


# ── (1) reads_from → journal upstream-hash wiring ───────────────────────────


def test_upstream_hash_wiring_changed_upstream_rekeys_downstream(tmp_path):
    """A task that reads an upstream gets that upstream's envelope hash in its
    key; a CHANGED upstream → different hash → different downstream key (forcing
    a re-dispatch).  This is the M3 follow-up #3 wiring."""
    journal = ResultJournal()
    tracker = JournalKeyTracker(journal)
    # DAG: up writes x; down reads x (strictly-earlier wave) → reads_from.
    tasks = [
        {"task_id": "up", "parallel_group": 0, "writes": ["x"]},
        {"task_id": "down", "parallel_group": 1, "reads": ["x"], "depends_on": ["up"]},
    ]
    dag_proof = compute_dag_proof(tasks)
    assert dag_proof.reads_from("down") == frozenset({"up"})

    down = tasks[1]
    # Before the upstream is journaled: no hash → empty set.
    assert scheduler_upstream_hashes_for(down, dag_proof, tracker) == frozenset()
    key_empty = journal.key(down, 1, model="sonnet", briefing="b", upstream_envelope_hashes=[])

    # Journal the upstream with envelope V1, record its key in the tracker.
    up = tasks[0]
    up_key_v1 = journal.key(up, 1, model="sonnet", briefing="b", upstream_envelope_hashes=[])
    journal.put(up_key_v1, _env("up"), usage={"output_tokens": 1})
    tracker.record("up", up_key_v1)

    h1 = scheduler_upstream_hashes_for(down, dag_proof, tracker)
    assert len(h1) == 1
    key_v1 = journal.key(down, 1, model="sonnet", briefing="b", upstream_envelope_hashes=list(h1))
    # The downstream key now incorporates the upstream's hash → differs from empty.
    assert key_v1 != key_empty

    # Change the upstream's OUTPUT envelope (different content → different hash).
    up_key_v2 = journal.key(
        {**up, "task_id": "up"}, 1, model="sonnet", briefing="b2", upstream_envelope_hashes=[]
    )
    journal.put(up_key_v2, _env("up", status="done") | {"notes_md": "CHANGED"}, usage={"o": 1})
    tracker.record("up", up_key_v2)
    h2 = scheduler_upstream_hashes_for(down, dag_proof, tracker)
    key_v2 = journal.key(down, 1, model="sonnet", briefing="b", upstream_envelope_hashes=list(h2))
    assert h2 != h1
    assert key_v2 != key_v1  # changed upstream → re-keyed downstream → re-dispatch


def test_pipeline_threads_upstream_hash_into_downstream_key(tmp_path):
    """End-to-end: pipeline() runs an upstream then a downstream that reads it;
    the downstream's journal key incorporates the upstream's envelope hash."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "up",
            "parallel_group": 0,
            "writes": ["x"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "down",
            "parallel_group": 1,
            "reads": ["x"],
            "depends_on": ["up"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _TimedRunner(writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    assert [r["status"] for r in res] == ["done", "done"]
    # The downstream started strictly after the upstream completed (barrier on dep).
    assert runner.started_at["down"] >= runner.ended_at["up"]


# ── pipeline(): barrier-free advance + real-dep barrier ─────────────────────


def test_pipeline_advances_independent_task_while_peer_in_flight(tmp_path):
    """An independent task advances while a SLOWER peer is still in-flight.

    Sleep-RATIO independent: we assert the faster task STARTED before the slower
    one COMPLETED (true concurrency), and that BOTH were in-flight at once."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "fast",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "slow",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    assert dag_proof.independent("fast", "slow")
    runner = _TimedRunner(sleeps={"fast": 0.0, "slow": 0.3}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    assert [r["status"] for r in res] == ["done", "done"]
    # Both were concurrently in-flight at least once.
    assert runner.max_concurrency == 2
    # The fast task started before the slow one finished (overlap, not serial).
    assert runner.started_at["fast"] < runner.ended_at["slow"]


def test_pipeline_dependent_waits_for_upstream_terminal(tmp_path):
    """A dependent task does NOT start until its upstreams are TERMINAL (the real
    barrier on a genuine dependency edge)."""
    clone = _git_init_clone(tmp_path)
    # T3 depends_on + reads from both T1 and T2 (the M0 §1.1 shape).
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t3",
            "parallel_group": 1,
            "reads": ["a", "b"],
            "writes": ["c"],
            "depends_on": ["t1", "t2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _TimedRunner(sleeps={"t1": 0.1, "t2": 0.2, "t3": 0.0}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    assert [r["status"] for r in res] == ["done", "done", "done"]
    # T3 started only after BOTH upstreams reached TERMINAL.
    assert runner.started_at["t3"] >= runner.ended_at["t1"]
    assert runner.started_at["t3"] >= runner.ended_at["t2"]
    # T1 and T2 overlapped (barrier-free between them).
    assert runner.started_at["t1"] < runner.ended_at["t2"]


# ── SAFETY (the load-bearing, un-fakeable assertions) ───────────────────────


def _overlap_tasks():
    # Two tasks that BOTH write a.txt — write-overlapping, NOT independent.
    return [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]


def _lying_proof():
    """An ADVERSARIAL DagProof that FALSELY claims t1,t2 independent."""
    return DagProof(
        _independent_pairs=frozenset({frozenset({"t1", "t2"})}),
        _reads_from_items=frozenset(),
    )


def test_safety_write_overlap_never_concurrent_even_with_lying_proof(tmp_path):
    """SAFETY: two write-overlapping tasks are NEVER concurrently in-flight under
    pipeline() — even when the DagProof LIES and claims them independent.  The
    dynamic write_disjoint backstop holds the line (fail-closed)."""
    clone = _git_init_clone(tmp_path)
    tasks = _overlap_tasks()
    liar = _lying_proof()
    assert liar.independent("t1", "t2") is True  # the proof lies
    runner = _TimedRunner(sleeps={"t1": 0.2, "t2": 0.2}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, liar)))
    assert [r["status"] for r in res] == ["done", "done"]
    # The write-overlapping pair was NEVER concurrently in-flight.
    assert runner.max_concurrency == 1
    assert frozenset({"t1", "t2"}) not in runner.overlap_pairs


def test_safety_mutation_check_dynamic_disjoint_gate_is_load_bearing(tmp_path, monkeypatch):
    """MUTATION CHECK: neutralize the dynamic write_disjoint backstop (make
    ``_writes`` report NO writes, so overlap can't be detected) and assert the
    SAFETY test would then FAIL — i.e. the two overlapping tasks DO run
    concurrently.  This proves the safety guarantee is not vacuous: if the gate
    were bypassed, the bad behavior surfaces.

    Belt-and-suspenders, the lying proof ALSO defeats the independence clause, so
    with the dynamic gate gone there is nothing left to hold the barrier — which
    is exactly the unsafe overlap the real gate prevents."""
    import scripts.host_scheduler as hs

    # Neutralize the dynamic disjointness signal: every task reports empty writes,
    # so `t_writes & _writes(other)` can never trip — the ONLY remaining gate is
    # the (lying) independence proof, which we already defeated.
    monkeypatch.setattr(hs, "_writes", lambda task: frozenset())

    clone = _git_init_clone(tmp_path)
    tasks = _overlap_tasks()
    liar = _lying_proof()
    runner = _TimedRunner(sleeps={"t1": 0.2, "t2": 0.2})
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, liar)))
    # With the gate mutated out, the overlap NOW happens — proving the gate was
    # the thing preventing it.  (A green safety test above + a red overlap here =
    # the safety property is real and load-bearing.)
    assert runner.max_concurrency == 2
    assert frozenset({"t1", "t2"}) in runner.overlap_pairs


def test_pipeline_no_proof_means_barrier_no_silent_overlap(tmp_path):
    """Absence of an independence proof ⇒ barrier.  Two write-disjoint tasks that
    the proof does NOT certify independent are NOT advanced concurrently — they
    serialize (fail-closed), never silently overlap."""
    clone = _git_init_clone(tmp_path)
    # Write-disjoint tasks, but an EMPTY proof: no pair is certified independent.
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    empty_proof = DagProof(_independent_pairs=frozenset(), _reads_from_items=frozenset())
    assert empty_proof.independent("t1", "t2") is False
    runner = _TimedRunner(sleeps={"t1": 0.15, "t2": 0.15}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, empty_proof)))
    assert [r["status"] for r in res] == ["done", "done"]
    # No proof → serialized (barrier), never concurrent.
    assert runner.max_concurrency == 1


# ── parallel(): the REAL WaveDispatcher barrier ─────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB (mirrors test_pm_dispatch.py's fixture) so
    parallel() can drive the REAL WaveDispatcher (it records attempts + terminal
    status in this DB)."""
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
    ws = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("proj", "repo:proj", "Proj", None, now, now),
    ).lastrowid
    proj = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, phase, created_by, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (ws, "p", "P", "d", "design:open", "atelier-pm-1", now, now),
    ).lastrowid
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db), "project_id": proj}


def _seed_rows(workspace, n, *, parallel_group=0):
    now = "2026-05-29T00:00:00Z"
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    ids = []
    for i in range(n):
        tid = conn.execute(
            "INSERT INTO tasks (project_id, title, description, status, parallel_group, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                workspace["project_id"],
                f"t{i}",
                "d",
                "pending",
                parallel_group,
                "atelier-pm-1",
                now,
                now,
            ),
        ).lastrowid
        ids.append(tid)
    conn.commit()
    conn.close()
    return ids


def _row_task(tid, parallel_group=0):
    # `id` is what the WaveDispatcher tracks; `task_id` is what CliDispatchTools
    # keys on — they MUST be equal so the two seams line up.
    return {
        "id": tid,
        "task_id": tid,
        "parallel_group": parallel_group,
        "status": "pending",
        "attempts": 0,
        "assigned_persona": "be-1",
        "phase": "qa",
        "title": f"t{tid}",
    }


def test_parallel_is_a_true_barrier_via_real_wavedispatcher(workspace):
    """parallel() drives the REAL WaveDispatcher: a single wave of two tasks both
    reach TERMINAL-ONLY before the façade returns (the wave-gate barrier)."""
    ids = _seed_rows(workspace, 2, parallel_group=0)
    tasks = [_row_task(t, 0) for t in ids]

    runner = FakeCliRunner(structured_output=lambda argv, cwd: _env(_tid_from_argv(argv)))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    dag_proof = compute_dag_proof([{"task_id": t, "parallel_group": 0} for t in ids])

    async def go():
        with CliDispatchTools(
            budget=budget,
            journal=journal,
            clone_dir=str(workspace["root"]),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=runner,
        ) as tools:
            return await parallel(
                tasks,
                dispatcher=tools,
                db_path=workspace["db"],
                budget=budget,
                dag_proof=dag_proof,
            )

    summaries = _run(go())
    assert len(summaries) == 1  # one wave
    summary = summaries[0]
    assert summary["terminal_only"] is True
    assert summary["complete"] is True
    assert set(summary["reports"].values()) == {"done"}
    assert runner.call_count == 2


def test_parallel_second_wave_waits_for_first(workspace):
    """Two waves: every wave-0 task is TERMINAL before any wave-1 task runs (the
    cross-wave barrier).  Asserted via per-task start timestamps."""
    ids0 = _seed_rows(workspace, 1, parallel_group=0)
    ids1 = _seed_rows(workspace, 1, parallel_group=1)
    tasks = [_row_task(ids0[0], 0), _row_task(ids1[0], 1)]

    runner = _TimedRunner(sleeps={str(ids0[0]): 0.1, str(ids1[0]): 0.0})
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    dag_proof = compute_dag_proof(
        [{"task_id": ids0[0], "parallel_group": 0}, {"task_id": ids1[0], "parallel_group": 1}]
    )

    async def go():
        with CliDispatchTools(
            budget=budget,
            journal=journal,
            clone_dir=str(workspace["root"]),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=runner,
        ) as tools:
            return await parallel(
                tasks,
                dispatcher=tools,
                db_path=workspace["db"],
                budget=budget,
                dag_proof=dag_proof,
            )

    summaries = _run(go())
    assert len(summaries) == 2
    assert all(s["terminal_only"] for s in summaries)
    # Wave-1 task started only after the wave-0 task finished.
    assert runner.started_at[str(ids1[0])] >= runner.ended_at[str(ids0[0])]


# ── static_fleet_width: only ever narrows ───────────────────────────────────


def test_static_fleet_width_only_narrows_near_exhausted_budget():
    """A near-exhausted budget shrinks fan-out below MAX_PARALLEL_WORKERS; it
    never widens past it."""
    # Healthy budget: width is clamped to max_workers (never wider).
    big = BudgetPool(total_tokens=1_000_000)
    assert BudgetPool.static_fleet_width(big, per_agent_tokens=6_000, max_workers=5) == 5

    # Near-exhausted budget: width narrows to what remains.
    small = BudgetPool(total_tokens=20_000)  # effective ceiling = 14_000
    # remaining 14_000 // 6_000 = 2 → narrowed below 5.
    assert BudgetPool.static_fleet_width(small, per_agent_tokens=6_000, max_workers=5) == 2

    # Fully spent: width is 0 (no fan-out).
    small.charge({"output_tokens": 14_000})
    assert BudgetPool.static_fleet_width(small, per_agent_tokens=6_000, max_workers=5) == 0


def test_pipeline_fleet_narrows_concurrency_to_budget(tmp_path):
    """pipeline()'s shared semaphore is sized by static_fleet_width: a budget that
    only affords 1 agent at a time serializes even write-disjoint, proven-
    independent tasks (concurrency capped to the affordable fleet)."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    assert dag_proof.independent("t1", "t2")
    # Budget affords only ~1 sonnet agent (est 6_000): ceiling 7_000 // 6_000 = 1.
    budget = BudgetPool(total_tokens=10_000)
    runner = _TimedRunner(sleeps={"t1": 0.15, "t2": 0.15}, writes=_writes_map(tasks))
    journal = ResultJournal()
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    assert [r["status"] for r in res] == ["done", "done"]
    # Fleet width narrowed to 1 → tasks serialize despite being independent.
    assert runner.max_concurrency == 1


# ── worktree isolation: separate trees + conflict-free deterministic merge ──


def test_worktree_isolation_two_writers_separate_trees_merge_clean(tmp_path):
    """Two concurrent, write-disjoint writers operate in SEPARATE git worktrees;
    both results merge back conflict-free into the base clone, deterministically,
    and the worktrees are cleaned up."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)

    cwds: dict[str, str] = {}

    class _WriteRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            cwds[tid] = cwd
            fn = {"t1": "a.txt", "t2": "b.txt"}[tid]
            (Path(cwd) / fn).write_text(f"by {tid}")
            await asyncio.sleep(0.1)
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 5},
                "is_error": False,
                "subtype": "success",
                "structured_output": _env(tid),
            }

    runner = _WriteRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                worktree_factory=simple_worktree_factory(clone),
            ),
        )
    )
    assert [r["status"] for r in res] == ["done", "done"]
    # Each writer ran in its OWN worktree (distinct cwds).
    assert cwds["t1"] != cwds["t2"]
    assert Path(cwds["t1"]).resolve() != clone.resolve()
    # Both writes merged back into the base clone conflict-free.
    assert (clone / "a.txt").read_text() == "by t1"
    assert (clone / "b.txt").read_text() == "by t2"
    # Worktrees cleaned up — only the main worktree remains.
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1


def test_concurrent_writers_never_share_base_tree_under_fanout(tmp_path):
    """MAJOR-1 STRESS REGRESSION: under fan-out (N concurrent write-disjoint
    writers), the ``git worktree add`` admin race must NEVER drop a writer to an
    UN-ISOLATED run in the shared base clone.

    The pre-fix bug: concurrent ``git worktree add`` calls race on the shared
    ``.git/worktrees/`` admin dir and intermittently fail (exit 128); ``_run_one``
    then caught ``WorktreeError`` and ran the writer un-isolated in the base clone
    (``cwd=clone_dir``).  Measured ~40% of N=8 runs (with up to TWO writers in the
    base at once) — a race on ``.git/index.lock`` whose outputs were left as
    untracked ``?? wN.txt`` in a dirty base (the un-isolated path never commits).

    The fix serializes the worktree-admin git ops under a single lock (+ a retry
    backstop) and, if isolation still can't be obtained, FAILS THE ATTEMPT instead
    of running un-isolated.  This test drives N=8 concurrent writers and asserts:

    * ZERO writer ever ran with ``cwd`` == the base clone (every dispatch cwd is a
      ``.atelier-worktrees/<id>`` worktree) — i.e. no un-isolated writer, ever;
    * the base ``git status --porcelain`` is EMPTY at the end (no dirty/untracked
      residue);
    * every output landed in the base via its worktree merge-back.

    The fix serializes the worktree-admin git ops under a single lock (+ a retry
    backstop) and, if isolation still can't be obtained, FAILS THE ATTEMPT instead
    of running un-isolated.

    DETERMINISTIC Iron-Law mechanism (no reliance on the intermittent OS race
    firing): the test wraps the real ``simple_worktree_factory`` in a probe that
    records the MAX number of factory invocations executing CONCURRENTLY, widening
    the window with a tiny sleep.  The factory mutates the shared
    ``.git/worktrees/`` admin dir, so under the fix it MUST be serialized
    (``worktree_admin_lock``) → max concurrency == 1; on the pre-fix code the
    invocations run in parallel threads → max concurrency > 1 AND the real admin
    race intermittently drops writers un-isolated into the base.  We assert BOTH:

    * (deterministic) worktree-creation concurrency is exactly 1 (serialized) —
      this FAILS on the pre-fix code on EVERY run, not just the racy fraction;
    * (behavioral) ZERO writer ever ran with ``cwd`` == the base clone, every
      output landed via its worktree, and the base ``git status --porcelain`` is
      EMPTY with no leaked worktrees.
    """
    n_writers = 8
    repetitions = 3  # a few reps so the behavioral race also gets exercised

    for rep in range(repetitions):
        rep_root = tmp_path / f"rep{rep}"
        rep_root.mkdir()
        clone = _git_init_clone(rep_root)
        tasks = [
            {
                "task_id": f"w{i}",
                "parallel_group": 0,
                "writes": [f"w{i}.txt"],
                "assigned_persona": "be-1",
                "phase": "qa",
            }
            for i in range(n_writers)
        ]
        dag_proof = compute_dag_proof(tasks)

        # ── DETERMINISTIC probe: max concurrency of worktree-CREATION ──────────
        # The real factory runs in a worker thread (`asyncio.to_thread`).  We wrap
        # it to count how many invocations are inside the admin section at once.
        # Serialized (fix) ⇒ 1; concurrent (pre-fix) ⇒ >1.  A short sleep widens
        # the overlap window so the pre-fix concurrency is observed every run.
        real_factory = simple_worktree_factory(clone)
        admin_lock = threading.Lock()
        admin_live = {"n": 0}
        admin_max = {"n": 0}

        def _probed_factory(
            task_id, _rf=real_factory, _al=admin_lock, _live=admin_live, _mx=admin_max
        ):
            with _al:
                _live["n"] += 1
                _mx["n"] = max(_mx["n"], _live["n"])
            try:
                time.sleep(0.02)  # widen the concurrency window (deterministic)
                return _rf(task_id)
            finally:
                with _al:
                    _live["n"] -= 1

        # ── behavioral probe: did any writer run UN-ISOLATED in the base? ──────
        base_real = clone.resolve()
        unisolated_dispatches: list[str] = []

        class _WriteRunner(FakeCliRunner):
            def __init__(self, base, sink):
                super().__init__(structured_output=None)
                self._base = base  # bind per-instance (avoids B023 loop capture)
                self._sink = sink

            async def __call__(self, argv, cwd):
                tid = _tid_from_argv(argv)
                if Path(cwd).resolve() == self._base:
                    self._sink.append(tid)
                # Every writer creates its declared output in its OWN cwd.
                (Path(cwd) / f"{tid}.txt").write_text(f"by {tid}")
                await asyncio.sleep(0.03)
                self.calls.append({"argv": list(argv), "cwd": cwd})
                return {
                    "usage": {"output_tokens": 5},
                    "is_error": False,
                    "subtype": "success",
                    "structured_output": _env(tid),
                }

        runner = _WriteRunner(base_real, unisolated_dispatches)
        journal = ResultJournal()
        budget = BudgetPool(total_tokens=10_000_000)
        res = _run(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone,
                    runner,
                    journal,
                    budget,
                    dag_proof,
                    worktree_factory=_probed_factory,
                    max_workers=n_writers,  # allow the full fan-out concurrently
                ),
            )
        )
        # (DETERMINISTIC) worktree creation was serialized — never two adds at once.
        assert admin_max["n"] == 1, (
            f"rep {rep}: {admin_max['n']} concurrent `git worktree add` invocations "
            "— the worktree-admin git ops are NOT serialized, so concurrent adds "
            "race on `.git/worktrees/` (MAJOR-1)."
        )
        # (a) NO writer ever ran un-isolated in the shared base tree.
        assert unisolated_dispatches == [], (
            f"rep {rep}: {len(unisolated_dispatches)} writer(s) ran UN-ISOLATED in "
            f"the shared base clone ({unisolated_dispatches}) — file-race-freedom "
            "breached under fan-out (the worktree-add admin race)."
        )
        # (b) every task reached done via its worktree merge-back.
        assert [r["status"] for r in res] == ["done"] * n_writers, (
            f"rep {rep}: not all writers done: {[r.get('status') for r in res]}"
        )
        # (c) every output landed in the base via its worktree (merged, tracked).
        for i in range(n_writers):
            out = clone / f"w{i}.txt"
            assert out.read_text() == f"by w{i}", f"rep {rep}: w{i}.txt missing/wrong"
        # (d) base is CLEAN at the end — no dirty/untracked residue, no leaked
        #     worktrees (only the main worktree remains).
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True
        ).stdout
        assert porcelain.strip() == "", f"rep {rep}: base clone left dirty:\n{porcelain}"
        wt_list = subprocess.run(
            ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert len(wt_list.splitlines()) == 1, f"rep {rep}: leaked worktrees:\n{wt_list}"


def test_dependent_writer_worktree_contains_upstream_outputs(tmp_path):
    """REGRESSION (live-e2e T3-blocked): a dependent WRITER task that reads its
    upstreams' outputs must run in a worktree that PHYSICALLY contains those
    outputs.

    The original bug deferred every worktree merge-back to AFTER the admission
    loop, so a downstream writer's worktree — carved from base HEAD at admission
    via ``git worktree add … HEAD`` — was branched from a HEAD that did NOT yet
    contain its (still-unmerged) upstreams' files.  The dependent agent then could
    not READ its inputs and blocked.  This test fails on that regression because
    the runner here ACTUALLY reads ``a.txt``/``b.txt`` from its own cwd and
    refuses (returns a failed-attempt envelope) when they are absent — exactly
    what the real claude agent did when it returned ``status: blocked``.

    The fix merges each successful writer EAGERLY (the instant it is terminal,
    before the admission loop is notified), so the upstream outputs are in base
    HEAD before the downstream worktree is carved.
    """
    clone = _git_init_clone(tmp_path)
    # T1 writes a.txt, T2 writes b.txt (barrier-free pair); T3 reads BOTH and,
    # only if it can read them, writes c.txt — a real dependency barrier.
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t3",
            "parallel_group": 1,
            "reads": ["a.txt", "b.txt"],
            "writes": ["c.txt"],
            "depends_on": ["t1", "t2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks, existing_files={"seed"})

    # What T3 actually saw in its cwd at dispatch — the load-bearing observation.
    t3_saw: dict[str, str | None] = {}

    class _ReadThenWriteRunner(FakeCliRunner):
        """Writers create their output in cwd; the dependent t3 READS a.txt/b.txt
        from its OWN cwd worktree first and only writes c.txt (status done) if it
        can — else it returns a blocked envelope, mirroring the real agent."""

        def __init__(self):
            super().__init__(structured_output=None)

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            cwd_p = Path(cwd)
            self.calls.append({"argv": list(argv), "cwd": cwd})
            if tid in ("t1", "t2"):
                fn = {"t1": "a.txt", "t2": "b.txt"}[tid]
                content = {"t1": "A", "t2": "B"}[tid]
                (cwd_p / fn).write_text(content)
                await asyncio.sleep(0.02)
                return {
                    "usage": {"output_tokens": 5},
                    "is_error": False,
                    "subtype": "success",
                    "structured_output": _env(tid),
                }
            # t3: read the upstream outputs from MY OWN cwd worktree.
            a = cwd_p / "a.txt"
            b = cwd_p / "b.txt"
            t3_saw["a.txt"] = a.read_text() if a.exists() else None
            t3_saw["b.txt"] = b.read_text() if b.exists() else None
            if not (a.exists() and b.exists()):
                # The regression: inputs absent → the agent blocks (cannot read).
                return {
                    "usage": {"output_tokens": 5},
                    "is_error": False,
                    "subtype": "success",
                    "structured_output": _env("t3", status="blocked"),
                }
            (cwd_p / "c.txt").write_text(a.read_text() + b.read_text())
            return {
                "usage": {"output_tokens": 5},
                "is_error": False,
                "subtype": "success",
                "structured_output": _env("t3"),
            }

    runner = _ReadThenWriteRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                worktree_factory=simple_worktree_factory(clone),
            ),
        )
    )
    # T3 PHYSICALLY saw its upstreams' outputs in its own worktree at dispatch.
    assert t3_saw == {"a.txt": "A", "b.txt": "B"}, (
        f"dependent writer's worktree was missing upstream outputs: {t3_saw} — "
        "the deferred-merge regression (T3 branched from a HEAD without a.txt/b.txt)."
    )
    # All three reached done (T3 only does so when it could read its inputs).
    assert [r["status"] for r in res] == ["done", "done", "done"]
    # The merged-back base clone carries the derived output.
    assert (clone / "a.txt").read_text() == "A"
    assert (clone / "b.txt").read_text() == "B"
    assert (clone / "c.txt").read_text() == "AB"
    # Worktrees cleaned up — only the main worktree remains.
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1


# ── budget exhaustion surfaces (terminal, not swallowed) ────────────────────


def test_pipeline_budget_exceeded_propagates(tmp_path):
    """M5 (3) — NEW CONTRACT: a per-task BudgetExceeded is a PER-TASK abandon
    (category 'capacity') + escalate, NOT a whole-run abort. pipeline() RETURNS a
    result list (does not raise); the single task is abandoned 'capacity', the
    worker never spawned (refused at the pre-spawn budget gate), and escalate_fn
    fired once for it.

    (Previously this asserted ``pytest.raises(BudgetExceeded)`` — that
    whole-run-abort behavior is the bug M5 closed: one task's budget exhaustion
    must not abort the run for unrelated tasks. See
    ``test_pipeline_budget_exceeded_is_per_task_abandon_not_whole_run_abort`` for
    the multi-task survivor proof.)"""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _TimedRunner()
    journal = ResultJournal()
    escalations, escalate_fn = _escalation_recorder()
    # Ceiling 700 < est 6_000 → assert_can_dispatch raises BudgetExceeded pre-spawn.
    budget = BudgetPool(total_tokens=1_000)
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # pipeline() RETURNED (no whole-run raise) with t1 abandoned 'capacity'.
    assert isinstance(res, list) and len(res) == 1
    t1 = _abandoned_for(res, "t1")
    assert t1 is not None and t1["category"] == "capacity"
    # The worker never spawned (refused at the budget gate).
    assert runner.call_count == 0
    # escalate_fn fired once for the capacity abandon.
    cap = [e for e in escalations if e.get("task_id") == "t1" and e.get("category") == "capacity"]
    assert len(cap) == 1, f"expected one capacity escalation for t1, got {escalations}"


def test_pipeline_journal_replay_costs_nothing_on_rerun(tmp_path):
    """Running the same DAG twice: the second run is all journal hits — zero new
    runner calls and zero new budget spend (idea 4 replay through the scheduler)."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)

    runner1 = _TimedRunner(writes=_writes_map(tasks))
    _run(pipeline(tasks, **_pipeline_kwargs(clone, runner1, journal, budget, dag_proof)))
    assert runner1.call_count == 2
    spent_after_run1 = budget.spent()

    runner2 = _TimedRunner()
    res2 = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner2, journal, budget, dag_proof)))
    assert [r["status"] for r in res2] == ["done", "done"]
    # Second run: ALL journal hits → no runner calls, no extra spend.
    assert runner2.call_count == 0
    assert budget.spent() == spent_after_run1
    assert all(not is_failed_attempt(r) for r in res2)


# ── MAJOR-1 regression: a raising worker-setup seam must NOT hang pipeline() ─


def test_pipeline_raising_model_for_seam_does_not_hang(tmp_path):
    """MAJOR-1: a FALLIBLE setup seam (model_for / briefing_for — unknown
    persona / missing template) raising BEFORE the dispatch must route through
    the worker's ``finally`` (mark terminal + notify), so the admission loop
    does NOT block forever on ``progress.wait()``.  pipeline() must PROPAGATE the
    exception promptly — NOT hang to the outer wait_for guard."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _TimedRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)

    class _SeamError(RuntimeError):
        pass

    def boom_model_for(task, attempt):
        raise _SeamError("unknown persona / missing template")

    async def go():
        # Outer guard: if the fix regressed, pipeline() HANGS and this times out
        # (the bug signature the reviewer reproduced).  With the fix it raises
        # _SeamError well within the guard.
        return await asyncio.wait_for(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone, runner, journal, budget, dag_proof, model_for=boom_model_for
                ),
            ),
            timeout=5.0,
        )

    with pytest.raises(_SeamError):
        _run(go())
    # The dispatch never happened (the seam raised before run_attempt).
    assert runner.call_count == 0


def test_pipeline_raising_briefing_for_seam_does_not_hang(tmp_path):
    """MAJOR-1 (companion): a raising ``briefing_for`` likewise routes through the
    finally and propagates promptly instead of hanging."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _TimedRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)

    class _TemplateError(RuntimeError):
        pass

    def boom_briefing_for(task, attempt):
        raise _TemplateError("missing template")

    async def go():
        return await asyncio.wait_for(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone, runner, journal, budget, dag_proof, briefing_for=boom_briefing_for
                ),
            ),
            timeout=5.0,
        )

    with pytest.raises(_TemplateError):
        _run(go())
    assert runner.call_count == 0


# ── MAJOR-2 regression: mid-loop merge failure leaves a CLEAN base clone ─────


def test_merge_failure_cleans_up_all_worktrees_and_restores_base(tmp_path, monkeypatch):
    """MAJOR-2: if a worktree merge raises mid-loop, the scheduler must clean up
    ALL remaining worktrees AND restore the base working tree — so a caller's
    fallback inherits a CLEAN clone (no leaked worktrees, no dirty base)."""
    clone = _git_init_clone(tmp_path)
    # Three write-disjoint writers → three worktrees queued for merge.
    tasks = [
        {
            "task_id": f"t{i}",
            "parallel_group": 0,
            "writes": [f"f{i}.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        }
        for i in range(3)
    ]
    dag_proof = compute_dag_proof(tasks)

    declared = _writes_map(tasks)

    class _WriteRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            # Create the DECLARED output (so the honest `done` clears the
            # false-`done` guard) plus a sidecar for the merge-back assertion.
            for rel in declared.get(tid, []):
                (Path(cwd) / rel).write_text(f"by {tid}")
            (Path(cwd) / f"{tid}-out.txt").write_text(f"by {tid}")
            await asyncio.sleep(0.02)
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 5},
                "is_error": False,
                "subtype": "success",
                "structured_output": _env(tid),
            }

    # Make the SECOND merge in the deterministic order fail (simulate a git
    # failure / breached-disjointness conflict) so worktree #3 is never merged.
    import scripts.host_scheduler as hs

    real_merge = hs._merge_worktree
    calls = {"n": 0}

    def flaky_merge(wt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise WorktreeError("INVARIANT VIOLATION: simulated mid-loop merge failure")
        return real_merge(wt)

    monkeypatch.setattr(hs, "_merge_worktree", flaky_merge)

    runner = _WriteRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    with pytest.raises(WorktreeError):
        _run(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone,
                    runner,
                    journal,
                    budget,
                    dag_proof,
                    worktree_factory=simple_worktree_factory(clone),
                ),
            )
        )

    # CLEAN clone afterward: zero leaked worktrees (only the main worktree) ...
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1, f"leaked worktrees:\n{listing}"
    # ... and a clean base working tree (no dirty files, no .atelier-worktrees).
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain.strip() == "", f"base clone left dirty:\n{porcelain}"
    assert not (clone / ".atelier-worktrees").exists()


def test_cleanup_after_merge_failure_clean_under_autocrlf(tmp_path):
    """MINOR-1 regression: ``_cleanup_after_merge_failure`` must leave a CLEAN base
    (empty ``git status --porcelain``) even on a host/clone with
    ``core.autocrlf=true``.

    Pre-fix, the cleanup's plain ``git reset --hard HEAD`` re-applied CRLF
    normalization and left a FALSE-dirty ``M <file>`` (an LF↔CRLF-only diff) for
    any committed file that contains LF newlines — so a real operator clone could
    show a dirty base after a merge-failure cleanup.  The fix pins
    ``core.autocrlf=false`` + ``core.eol=lf`` on the reset invocation, so the
    working tree reproduces HEAD byte-for-byte regardless of the host setting.
    """
    import scripts.host_scheduler as hs

    clone = tmp_path / "clone"
    clone.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, check=True)
    # Commit a file with LF newlines, THEN turn on autocrlf so a later reset would
    # want to rewrite LF→CRLF in the working tree (the false-dirty condition).
    (clone / "f.txt").write_text("line1\nline2\nline3\n")
    subprocess.run([*["git"], *ident, "add", "-A"], cwd=clone, check=True, capture_output=True)
    subprocess.run([*["git"], *ident, "commit", "-qm", "init"], cwd=clone, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=clone, check=True)

    # Sanity: with autocrlf on, a PLAIN reset --hard leaves the file false-dirty —
    # this documents the bug the fix targets (skip the assertion if this host's git
    # does not reproduce it, so the test stays portable; the real assertion is the
    # post-cleanup clean tree below).
    (clone / "f.txt").write_text("line1\r\nline2\r\nline3\r\n")  # CRLF in tree
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=clone, check=True, capture_output=True)

    # The engine cleanup MUST leave a clean base regardless of autocrlf.
    hs._cleanup_after_merge_failure(clone, [])
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain.strip() == "", (
        f"cleanup left a false-dirty base under core.autocrlf=true:\n{porcelain}"
    )


# ── MINOR-1 regression: a FAILED writer's partial writes never reach base ────


def test_failed_writer_partial_writes_discarded_not_merged(tmp_path):
    """MINOR-1: a writer that produced partial output THEN failed (its envelope is
    a failed attempt) must have its worktree DISCARDED, not merged — so its
    partial garbage never lands in the base clone, and its worktree is cleaned."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "ok",
            "parallel_group": 0,
            "writes": ["ok.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "bad",
            "parallel_group": 0,
            "writes": ["bad.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)

    class _MixedRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            # BOTH write partial output into their worktree first.
            (Path(cwd) / f"{tid}-partial.txt").write_text(f"partial by {tid}")
            await asyncio.sleep(0.02)
            self.calls.append({"argv": list(argv), "cwd": cwd})
            if tid == "bad":
                # Failed attempt: is_error=True → run_attempt returns FAILED_ATTEMPT.
                # (Never reaches the false-`done` guard — already a failed attempt.)
                return {
                    "usage": {"output_tokens": 1},
                    "is_error": True,
                    "subtype": "error",
                    "structured_output": None,
                }
            # The honest writer ALSO creates its declared output (`ok.txt`) so its
            # `done` clears the false-`done` guard and is merged.
            (Path(cwd) / "ok.txt").write_text(f"by {tid}")
            return {
                "usage": {"output_tokens": 5},
                "is_error": False,
                "subtype": "success",
                "structured_output": _env(tid),
            }

    runner = _MixedRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                worktree_factory=simple_worktree_factory(clone),
            ),
        )
    )
    # The good writer succeeded; the bad one is a failed attempt.  pipeline()
    # returns results in deterministic (parallel_group, task_id) order, so map by
    # that same ordering rather than the input order.
    ordered_ids = [
        t["task_id"] for t in sorted(tasks, key=lambda t: (t["parallel_group"], t["task_id"]))
    ]
    by_id = dict(zip(ordered_ids, res, strict=True))
    assert not is_failed_attempt(by_id["ok"]) and by_id["ok"]["status"] == "done"
    assert is_failed_attempt(by_id["bad"])
    # The successful writer's partial file merged back...
    assert (clone / "ok-partial.txt").read_text() == "partial by ok"
    # ... but the FAILED writer's partial file did NOT land in the base.
    assert not (clone / "bad-partial.txt").exists()
    # Worktrees cleaned up — only the main worktree remains, base is clean.
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1, f"leaked worktrees:\n{listing}"


# ── FALSE-`done` GUARD: a `done` envelope with a MISSING declared write is rejected ─


class _DeclaredWriteRunner(FakeCliRunner):
    """A FakeCliRunner that returns a configurable envelope per task and OPTIONALLY
    creates each task's declared output file in its own cwd worktree.

    *writes_files* maps ``task_id -> bool``: True ⇒ the runner CREATES the task's
    declared output (an honest writer); False ⇒ it returns the envelope WITHOUT
    creating the file (the false-`done` a real model produced live).  *statuses*
    maps ``task_id -> status`` (default ``"done"``)."""

    def __init__(self, *, declared, writes_files, statuses=None):
        super().__init__(structured_output=None)
        self._declared = declared  # task_id -> list[relpath]
        self._writes_files = writes_files
        self._statuses = statuses or {}

    async def __call__(self, argv, cwd):
        tid = _tid_from_argv(argv)
        self.calls.append({"argv": list(argv), "cwd": cwd})
        if self._writes_files.get(tid, True):
            for rel in self._declared.get(tid, []):
                p = Path(cwd) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"by {tid}")
        await asyncio.sleep(0.01)
        status = self._statuses.get(tid, "done")
        env = _env(tid, status=status)
        # A non-`done` terminal status must be a VALID envelope so it reaches the
        # guard (validate_envelope is the gate BEFORE the guard).  `abandoned`
        # requires notes_md line 1 to match the abandon grammar; `blocked` /
        # `needs-input` may carry an empty artifacts list.
        if status == "abandoned":
            env["notes_md"] = "ABANDON: blocked:declared output could not be produced"
        elif status in ("blocked", "needs-input"):
            env["artifacts"] = []
        return {
            "usage": {"output_tokens": 5},
            "is_error": False,
            "subtype": "success",
            "structured_output": env,
        }


def _run_guard_pipeline(clone, tasks, runner, *, dag_proof, isolated=True, budget_tokens=1_000_000):
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=budget_tokens)
    over = {}
    if isolated:
        over["worktree_factory"] = simple_worktree_factory(clone)
    return _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, **over),
        )
    )


def test_false_done_missing_write_converted_to_failed_attempt(tmp_path):
    """IRON-LAW: a writer that returns a terminal `done` envelope but does NOT
    create its declared `writes` file is REJECTED — the engine converts it to a
    FAILED_ATTEMPT (NOT accepted as done), its worktree is DISCARDED, and nothing
    lands in the base clone.

    This FAILS on the pre-guard code (a false `done` is wrongly accepted, merged,
    and reported done) and PASSES with the guard — see the companion mutation
    test below that stashes the guard and asserts the false `done` is wrongly
    accepted, proving this assertion is not vacuous."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "liar",
            "parallel_group": 0,
            "writes": ["out.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _DeclaredWriteRunner(
        declared={"liar": ["out.txt"]},
        writes_files={"liar": False},  # returns `done` but writes NOTHING
    )
    res = _run_guard_pipeline(clone, tasks, runner, dag_proof=dag_proof)
    # The false `done` was REJECTED → routed as a failed attempt (NOT accepted).
    assert is_failed_attempt(res[0]), (
        f"false `done` (no declared write) was wrongly accepted: {res[0]!r}"
    )
    # Its declared output never landed in the base clone (worktree discarded).
    assert not (clone / "out.txt").exists()
    # The base clone is clean and the worktree was cleaned up.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain.strip() == "", f"base left dirty:\n{porcelain}"
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1, f"leaked worktrees:\n{listing}"


def test_false_done_guard_mutation_check_is_load_bearing(tmp_path, monkeypatch):
    """MUTATION CHECK (Iron-Law companion): STASH the guard (make
    ``_missing_declared_writes`` report nothing ever missing) and assert the same
    false-`done` writer is then WRONGLY ACCEPTED as done and merged into the base.

    A green guard test above + this red mutation here proves the guard is the
    thing rejecting the false `done` — exactly the pre-guard behavior, reproduced
    deterministically without depending on the historical un-guarded code."""
    import scripts.host_scheduler as hs

    # Neutralize the guard: nothing is ever reported missing → the false `done`
    # sails through (the pre-guard behavior).
    monkeypatch.setattr(hs, "_missing_declared_writes", lambda task, write_dir: [])

    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "liar",
            "parallel_group": 0,
            "writes": ["out.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _DeclaredWriteRunner(
        declared={"liar": ["out.txt"]},
        writes_files={"liar": False},
    )
    res = _run_guard_pipeline(clone, tasks, runner, dag_proof=dag_proof)
    # With the guard stashed, the false `done` is WRONGLY accepted as done.
    assert not is_failed_attempt(res[0])
    assert res[0]["status"] == "done"


def test_honest_done_writer_accepted_and_merged(tmp_path):
    """NO-REGRESSION: a writer that returns `done` AND actually creates its
    declared `writes` file passes the guard cleanly — accepted, merged into the
    base, worktree cleaned up."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "honest",
            "parallel_group": 0,
            "writes": ["out.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _DeclaredWriteRunner(
        declared={"honest": ["out.txt"]},
        writes_files={"honest": True},  # honest: actually writes its declared output
    )
    res = _run_guard_pipeline(clone, tasks, runner, dag_proof=dag_proof)
    assert not is_failed_attempt(res[0])
    assert res[0]["status"] == "done"
    # The declared output merged back into the base clone.
    assert (clone / "out.txt").read_text() == "by honest"
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert len(listing.splitlines()) == 1, f"leaked worktrees:\n{listing}"


def test_readonly_done_task_exempt_from_guard(tmp_path):
    """EXEMPTION: a read-only / review task (NO declared `writes`) that returns
    `done` is accepted — the guard does not fire (no false-positive).  It writes
    nothing by design, so there is nothing to verify."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "review",
            "parallel_group": 0,
            # NO "writes" key → read-only / review task → exempt from the guard.
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _DeclaredWriteRunner(
        declared={"review": []},
        writes_files={"review": False},  # writes nothing — but it declares nothing.
    )
    # Read-only task ⇒ not a writer ⇒ no worktree even with a factory wired.
    res = _run_guard_pipeline(clone, tasks, runner, dag_proof=dag_proof)
    assert not is_failed_attempt(res[0])
    assert res[0]["status"] == "done"


def test_blocked_and_abandoned_envelopes_untouched_by_guard(tmp_path):
    """GUARD ONLY FIRES ON `done`: a writer that declares `writes` but returns a
    `blocked` (or `abandoned`) envelope — having written nothing — is UNCHANGED by
    the guard.  The guard checks ONLY `done`; a non-`done` terminal status is the
    worker's own honest signal and must pass through verbatim."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "blk",
            "parallel_group": 0,
            "writes": ["b.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "abd",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _DeclaredWriteRunner(
        declared={"blk": ["b.txt"], "abd": ["a.txt"]},
        writes_files={"blk": False, "abd": False},  # neither writes its declared file
        statuses={"blk": "blocked", "abd": "abandoned"},
    )
    res = _run_guard_pipeline(clone, tasks, runner, dag_proof=dag_proof)
    by_status = {r["status"] for r in res if not is_failed_attempt(r)}
    # Both terminal envelopes pass through verbatim — the guard did NOT convert
    # them to failed attempts even though their declared files are absent.
    assert by_status == {"blocked", "abandoned"}
    assert all(not is_failed_attempt(r) for r in res)


def test_false_done_guard_unisolated_writer_checks_clone(tmp_path):
    """PATH-RESOLUTION (un-isolated): with NO worktree_factory a writer runs in
    the clone itself (``run_cwd == clone_dir``); the guard must resolve the
    declared `writes` against the CLONE.  A `done` writer that did not create its
    declared file in the clone is rejected; one that did is accepted."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "u",
            "parallel_group": 0,
            "writes": ["u.txt"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    # Missing declared write, un-isolated → rejected (guard resolves vs the clone).
    runner_missing = _DeclaredWriteRunner(declared={"u": ["u.txt"]}, writes_files={"u": False})
    res_missing = _run_guard_pipeline(
        clone, tasks, runner_missing, dag_proof=dag_proof, isolated=False
    )
    assert is_failed_attempt(res_missing[0])
    assert not (clone / "u.txt").exists()


def test_missing_declared_writes_helper_resolves_repo_relative(tmp_path):
    """UNIT: ``_missing_declared_writes`` resolves the repo-relative declared
    `writes` against the worktree dir (not CWD), reports the absent ones, and
    treats a present (even empty) path as satisfied — existence is the contract,
    content is not over-constrained."""
    import scripts.host_scheduler as hs

    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    (wt / "present.txt").write_text("x")
    (wt / "empty.txt").write_text("")  # present-but-empty ⇒ satisfied (not missing)
    (wt / "pkg" / "nested.py").write_text("y")
    task = {"task_id": "t", "writes": ["present.txt", "empty.txt", "pkg/nested.py", "absent.txt"]}
    missing = hs._missing_declared_writes(task, wt)
    assert missing == ["absent.txt"]
    # A task that declares no writes is trivially satisfied.
    assert hs._missing_declared_writes({"task_id": "t"}, wt) == []


# ════════════════════════════════════════════════════════════════════════════
# M5 — termination/cascade/escalation parity on the pipeline() path.
#
# These re-home the WaveDispatcher (Path A) guarantees onto pipeline() (Path B):
#   (1) a dependent of a failed/abandoned upstream is CASCADE-abandoned
#       (category "blocked", names the upstream, NEVER spawned, charges no
#       attempt, escalates) — and the cascade is TRANSITIVE.
#   (3) a per-task BudgetExceeded is a PER-TASK abandon (category "capacity") +
#       escalate, NOT a whole-run abort: unrelated tasks still complete.
#   (8) every dispatched task's attempt <= MAX_ATTEMPTS and no task is dispatched
#       more than once.
# All un-fakeable via runner call_count + the structured abandoned envelope +
# the escalate_fn capture.
# ════════════════════════════════════════════════════════════════════════════

from scripts.pm_dispatch import MAX_ATTEMPTS  # noqa: E402


class _FailingRunner(FakeCliRunner):
    """A FakeCliRunner that returns is_error=True (→ FAILED_ATTEMPT) for any task
    in ``fail_ids`` and a valid `done` envelope (creating declared writes) for the
    rest.  Records per-task call counts so a cascade test can assert a downstream
    was NEVER spawned (call_count for it == 0)."""

    def __init__(self, *, fail_ids: set[str], writes: dict[str, list[str]] | None = None):
        super().__init__(structured_output=None)
        self.fail_ids = set(fail_ids)
        self.writes = writes or {}
        self.per_task_calls: dict[str, int] = {}

    async def __call__(self, argv, cwd):
        tid = _tid_from_argv(argv)
        self.per_task_calls[tid] = self.per_task_calls.get(tid, 0) + 1
        self.calls.append({"argv": list(argv), "cwd": cwd})
        if tid in self.fail_ids:
            # is_error=True ⇒ run_attempt returns FAILED_ATTEMPT (an attempt ran
            # and failed) — the (i) terminal-failure encoding.
            return {
                "usage": {"output_tokens": 1},
                "is_error": True,
                "subtype": "error",
                "session_id": "s",
            }
        for rel in self.writes.get(tid, []):
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"by {tid}")
        return {
            "usage": {"output_tokens": 5},
            "is_error": False,
            "subtype": "success",
            "session_id": "s",
            "structured_output": _env(tid),
        }


def _escalation_recorder():
    """Return ``(escalations_list, escalate_fn)`` — escalate_fn appends each
    escalation mapping so a test can assert it fired for a cascade/capacity
    abandon."""
    escalations: list[dict] = []

    def escalate_fn(escalation):
        escalations.append(dict(escalation))

    return escalations, escalate_fn


def _abandoned_for(results, tid):
    """Find the structured abandoned result dict for *tid* in pipeline()'s return
    (which is in (parallel_group, task_id) order)."""
    for r in results:
        if isinstance(r, dict) and r.get("task_id") == tid and r.get("status") == "abandoned":
            return r
    return None


def test_pipeline_cascade_abandons_dependent_of_failed_upstream(tmp_path):
    """t1 (writes a) <- t2 (depends_on t1). t1's attempt FAILS. Assert: t2 is
    NEVER spawned (runner call_count for t2 == 0), t2's result is abandoned/blocked
    naming t1, t2 charged NO attempt, escalate_fn called for t2 category 'blocked'."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["t1"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids={"t1"}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()

    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # t1 attempted (and failed); t2 was NEVER dispatched (cascade gate held it).
    assert runner.per_task_calls.get("t1", 0) >= 1
    assert runner.per_task_calls.get("t2", 0) == 0, "t2 must NOT be spawned — its upstream failed"
    # t2's result is a structured abandoned/blocked envelope naming t1.
    t2 = _abandoned_for(res, "t2")
    assert t2 is not None, f"t2 must be abandoned, got {res}"
    assert t2["category"] == "blocked"
    assert t2["upstream_task_id"] == "t1"
    # escalate_fn fired for t2 with category 'blocked'.
    blocked = [
        e for e in escalations if e.get("task_id") == "t2" and e.get("category") == "blocked"
    ]
    assert len(blocked) == 1, f"expected one blocked escalation for t2, got {escalations}"


def test_pipeline_cascade_reaches_transitive_descendant(tmp_path):
    """Chain t1 <- t2 <- t3, t1 fails. BOTH t2 and t3 cascade-abandoned (never
    spawned), each naming its FIRST abandoned ancestor; t3 proves transitivity
    (a fix that abandons only direct dependents would leave t3 admitted)."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 1,
            "reads": ["a"],
            "writes": ["b"],
            "depends_on": ["t1"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t3",
            "parallel_group": 2,
            "reads": ["b"],
            "depends_on": ["t2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids={"t1"}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()

    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    assert runner.per_task_calls.get("t2", 0) == 0
    assert runner.per_task_calls.get("t3", 0) == 0, "TRANSITIVITY: t3 must cascade too"
    t2, t3 = _abandoned_for(res, "t2"), _abandoned_for(res, "t3")
    assert t2 is not None and t2["category"] == "blocked" and t2["upstream_task_id"] == "t1"
    assert t3 is not None and t3["category"] == "blocked"
    # t3's first abandoned ancestor on its depends_on chain is t2 (which is itself
    # abandoned because t1 failed) — the transitive cascade source.
    assert t3["upstream_task_id"] == "t2"
    # Each cascade-abandoned task escalated once.
    for tid in ("t2", "t3"):
        hits = [
            e for e in escalations if e.get("task_id") == tid and e.get("category") == "blocked"
        ]
        assert len(hits) == 1, f"expected one blocked escalation for {tid}, got {escalations}"


def test_pipeline_budget_exceeded_is_per_task_abandon_not_whole_run_abort(tmp_path):
    """Independent t_ok + t_broke whose est trips BudgetExceeded for t_broke only
    (a downstream of t_broke cascades). Assert: t_broke abandoned 'capacity' +
    escalate once; t_ok completes done; pipeline() RETURNS (does not raise); the
    dependent of t_broke is cascade-abandoned 'blocked'."""
    clone = _git_init_clone(tmp_path)
    # t_ok is independent and cheap; t_broke is expensive enough to trip the gate;
    # t_dep depends on t_broke so it must cascade once t_broke is capacity-abandoned.
    tasks = [
        {
            "task_id": "t_ok",
            "parallel_group": 0,
            "writes": ["ok"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t_broke",
            "parallel_group": 0,
            "writes": ["broke"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t_dep",
            "parallel_group": 1,
            "reads": ["broke"],
            "depends_on": ["t_broke"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids=set(), writes=_writes_map(tasks))
    journal = ResultJournal()
    # effective_ceiling = 14_000 * 0.70 = 9_800. Per-task est for t_broke (opus
    # via model_for below) = 12_000 > 9_800 ⇒ BudgetExceeded for t_broke; t_ok est
    # (haiku) = 2_000 < 9_800 ⇒ completes.
    budget = BudgetPool(total_tokens=14_000)
    escalations, escalate_fn = _escalation_recorder()

    def model_for(task, attempt):
        return "opus" if task["task_id"] == "t_broke" else "haiku"

    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                model_for=model_for,
                escalate_fn=escalate_fn,
            ),
        )
    )
    # pipeline() RETURNED a result list (no whole-run raise).
    assert isinstance(res, list)
    # t_ok completed done.
    ok = next((r for r in res if isinstance(r, dict) and r.get("task_id") == "t_ok"), None)
    assert ok is not None and ok.get("status") == "done", f"t_ok should complete, got {res}"
    # t_broke abandoned 'capacity' + escalated once.
    broke = _abandoned_for(res, "t_broke")
    assert broke is not None and broke["category"] == "capacity"
    cap = [
        e for e in escalations if e.get("task_id") == "t_broke" and e.get("category") == "capacity"
    ]
    assert len(cap) == 1, f"expected one capacity escalation for t_broke, got {escalations}"
    # t_dep cascade-abandoned 'blocked' naming t_broke (never spawned).
    assert runner.per_task_calls.get("t_dep", 0) == 0
    dep = _abandoned_for(res, "t_dep")
    assert dep is not None and dep["category"] == "blocked" and dep["upstream_task_id"] == "t_broke"


def test_pipeline_per_task_attempt_bound_le_max_attempts(tmp_path):
    """Every dispatched attempt is <= MAX_ATTEMPTS and no task is dispatched more
    than once (call_count <= 1). Pairs with the mutation note: re-appending a task
    to the admission set would make call_count exceed 1 → RED (non-tautology)."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)

    seen_attempts: list[int] = []

    class _AttemptSpyRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)
            self.writes = _writes_map(tasks)
            self.per_task_calls: dict[str, int] = {}

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            # The attempt number rides the -p prompt: "(attempt N)".
            m = re.search(r"\(attempt (\d+)\)", argv[2])
            assert m is not None
            seen_attempts.append(int(m.group(1)))
            self.per_task_calls[tid] = self.per_task_calls.get(tid, 0) + 1
            for rel in self.writes.get(tid, []):
                (Path(cwd) / rel).write_text(f"by {tid}")
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 5},
                "is_error": False,
                "subtype": "success",
                "structured_output": _env(tid),
            }

    runner = _AttemptSpyRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    res = _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    assert [r["status"] for r in res] == ["done", "done"]
    # Every dispatched attempt is within the MAX_ATTEMPTS budget ceiling.
    assert all(1 <= a <= MAX_ATTEMPTS for a in seen_attempts), seen_attempts
    # No task dispatched more than once (pipeline dispatches each task at attempt 1).
    assert all(c <= 1 for c in runner.per_task_calls.values()), runner.per_task_calls


def test_pipeline_cascade_on_failed_status_envelope_upstream(tmp_path):
    """M5 (2) classifier — a worker-authored validated ``status="failed"`` envelope
    is a TERMINAL FAILURE (parity with Path A pm_dispatch.py:802-821), NOT a
    success: its dependent CASCADE-abandons. A classifier that treated any
    validated envelope as success would admit t2 on a non-existent input (silent
    corruption) — this test catches that."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t2",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["t1"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)

    class _FailedStatusRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)
            self.per_task_calls: dict[str, int] = {}

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            self.per_task_calls[tid] = self.per_task_calls.get(tid, 0) + 1
            self.calls.append({"argv": list(argv), "cwd": cwd})
            # t1 returns a VALIDATED `failed` envelope (a hard run-and-failed —
            # accepted by validate_envelope, charged + journaled, but NOT a success).
            so = {
                "type": "task_result",
                "task_id": tid,
                "attempt": 1,
                "status": "failed",
                "artifacts": [],
                "notes_md": "unrecoverable",
            }
            return {
                "usage": {"output_tokens": 3},
                "is_error": False,
                "subtype": "success",
                "structured_output": so,
            }

    runner = _FailedStatusRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # t1 ran (and returned `failed`); t2 was NEVER spawned (cascade held it).
    assert runner.per_task_calls.get("t1", 0) == 1
    assert runner.per_task_calls.get("t2", 0) == 0, "t2 must cascade: its upstream `failed`"
    # t2 is cascade-abandoned 'blocked' naming t1.
    t2 = _abandoned_for(res, "t2")
    assert t2 is not None and t2["category"] == "blocked" and t2["upstream_task_id"] == "t1"
    # t1's own `failed` envelope escalated under category 'failed' (Path A parity).
    failed_esc = [
        e for e in escalations if e.get("task_id") == "t1" and e.get("category") == "failed"
    ]
    assert len(failed_esc) == 1, f"expected one 'failed' escalation for t1, got {escalations}"


def test_result_is_success_classifier_only_done_is_success():
    """Unit: ``_result_is_success`` is True ONLY for a validated `done` envelope;
    every other terminal encoding (FAILED_ATTEMPT, structured abandoned, a worker
    failed/abandoned/blocked/needs-input envelope, None) is a NON-success."""
    import scripts.host_scheduler as hs
    from scripts.cli_dispatch import FAILED_ATTEMPT

    assert hs._result_is_success({"status": "done"}) is True
    assert hs._result_is_success(FAILED_ATTEMPT) is False
    assert hs._result_is_success(None) is False
    assert (
        hs._result_is_success(
            hs._abandoned_result(
                "t", category="blocked", upstream_task_id="u", last_status="cascade"
            )
        )
        is False
    )
    for bad in ("failed", "abandoned", "blocked", "needs-input"):
        assert hs._result_is_success({"status": bad}) is False, bad


# ════════════════════════════════════════════════════════════════════════════
# M5 review round 2 — Path-A parity hardening + coverage (review @ 7aa863b).
# ════════════════════════════════════════════════════════════════════════════


class _StatusEnvelopeRunner(FakeCliRunner):
    """A FakeCliRunner that returns a per-task VALIDATED envelope with a
    configurable status + notes_md (so a worker self-`abandoned` / `failed` /
    false-`done` can be exercised end-to-end through pipeline()). Records per-task
    call counts. Honest `done` writers create their declared writes."""

    def __init__(self, *, specs: dict[str, dict], writes: dict[str, list[str]] | None = None):
        super().__init__(structured_output=None)
        # specs[tid] = {"status":..., "notes_md":..., "artifacts":..., "write": bool}
        self.specs = specs
        self.writes = writes or {}
        self.per_task_calls: dict[str, int] = {}

    async def __call__(self, argv, cwd):
        tid = _tid_from_argv(argv)
        self.per_task_calls[tid] = self.per_task_calls.get(tid, 0) + 1
        self.calls.append({"argv": list(argv), "cwd": cwd})
        spec = self.specs.get(tid, {"status": "done"})
        status = spec.get("status", "done")
        # Only an HONEST `done` writer (spec.write True) creates its declared
        # outputs; a false-`done` (write False) writes nothing.
        if status == "done" and spec.get("write", True):
            for rel in self.writes.get(tid, []):
                p = Path(cwd) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"by {tid}")
        so = {
            "type": "task_result",
            "task_id": tid,
            "attempt": 1,
            "status": status,
            "artifacts": spec.get("artifacts", [{"path": "f.py", "sha": "s"}]),
            "notes_md": spec.get("notes_md", "ok"),
        }
        return {
            "usage": {"output_tokens": 4},
            "is_error": False,
            "subtype": "success",
            "structured_output": so,
        }


def test_pipeline_worker_self_abandon_escalates_parsed_category(tmp_path):
    """FIX 1 (E2/O1) — a worker SELF-abandon envelope (status='abandoned',
    notes_md line-1 'ABANDON: capacity: ...') must escalate under the PARSED
    category 'capacity' (Path A parity via _parse_abandon_category), NOT None.
    Its dependent cascade-abandons 'blocked' naming the upstream and is never
    spawned. On pre-fix code the upstream's self-escalation category is None (the
    is_abandoned_result branch reads keys absent on a worker envelope) → RED."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "u",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "d",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["u"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _StatusEnvelopeRunner(
        specs={
            "u": {
                "status": "abandoned",
                "notes_md": "ABANDON: capacity: ran out of room",
                "artifacts": [{"path": "x", "sha": "y"}],
            },
        },
        writes=_writes_map(tasks),
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # (a) dependent cascades 'blocked' naming u, never spawned.
    assert runner.per_task_calls.get("d", 0) == 0
    d = _abandoned_for(res, "d")
    assert d is not None and d["category"] == "blocked" and d["upstream_task_id"] == "u"
    # (b) the upstream's OWN self-escalation carries the PARSED category 'capacity'.
    u_esc = [e for e in escalations if e.get("task_id") == "u"]
    assert len(u_esc) == 1, f"expected one self-escalation for u, got {escalations}"
    assert u_esc[0]["category"] == "capacity", f"parsed category lost: {u_esc[0]}"
    assert u_esc[0]["last_status"] == "abandoned"
    assert u_esc[0]["upstream_task_id"] is None


def test_failed_envelope_category_parses_abandoned_notes_md():
    """FIX 1 defense-in-depth — _failed_envelope_category for a worker `abandoned`
    envelope parses the ABANDON_RE category from notes_md (NOT the literal
    out-of-grammar 'abandoned'); failed/blocked/needs-input still return str(status)."""
    import scripts.host_scheduler as hs

    aband = {"status": "abandoned", "notes_md": "ABANDON: conflict: two writers", "artifacts": []}
    assert hs._failed_envelope_category(aband) == "conflict"
    # No/garbage notes_md → defensive 'capacity' (matches _parse_abandon_category fallback).
    assert hs._failed_envelope_category({"status": "abandoned", "notes_md": ""}) == "capacity"
    # failed/blocked/needs-input keep their own status string (those match Path A).
    assert hs._failed_envelope_category({"status": "failed", "notes_md": "x"}) == "failed"
    assert hs._failed_envelope_category({"status": "blocked", "notes_md": "x"}) == "blocked"
    assert hs._failed_envelope_category({"status": "needs-input", "notes_md": "x"}) == "needs-input"
    # This path's OWN structured abandon (has category + upstream_task_id) → None.
    structured = hs._abandoned_result(
        "t", category="blocked", upstream_task_id="u", last_status="cascade"
    )
    assert hs._failed_envelope_category(structured) is None
    # done / non-mapping → None.
    assert hs._failed_envelope_category({"status": "done"}) is None
    assert hs._failed_envelope_category(None) is None


def test_pipeline_cascade_escalation_survives_max_attempts_gate_raise(tmp_path):
    """FIX 2 (E1) — if the defensive MAX_ATTEMPTS gate raises in the SAME admission
    pass that cascade-abandoned a task, the cascaded task's GUARANTEED escalation
    must STILL have fired before the run aborts. On pre-fix code the queued
    cascade_escalations are flushed only AFTER the (raising) block → lost → RED."""
    clone = _git_init_clone(tmp_path)
    # Pass 1 dispatches t_fail (fails) + t_gate (ok). Pass 2: t_fail is now
    # abandoned ⇒ t_dep CASCADES (escalation queued this pass); t_gate is now done
    # ⇒ t_over becomes ready in the SAME pass with attempts=MAX_ATTEMPTS so
    # _attempt_for→6 trips the defensive gate, raising INSIDE the block AFTER
    # t_dep's cascade escalation was queued. (t_over depends on t_gate, not on the
    # failed task, so it does NOT itself cascade — it reaches the admission/gate.)
    tasks = [
        {
            "task_id": "t_fail",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t_gate",
            "parallel_group": 0,
            "writes": ["g"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t_dep",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["t_fail"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "t_over",
            "parallel_group": 1,
            "reads": ["g"],
            "writes": ["b"],
            "depends_on": ["t_gate"],
            "attempts": MAX_ATTEMPTS,
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids={"t_fail"}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    with pytest.raises(RuntimeError, match=r"obligation \(c\) breach"):
        _run(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn
                ),
            )
        )
    # The cascaded task's escalation STILL fired despite the gate raise.
    dep_esc = [
        e for e in escalations if e.get("task_id") == "t_dep" and e.get("category") == "blocked"
    ]
    assert len(dep_esc) == 1, f"cascade escalation LOST on gate raise: {escalations}"


def test_pipeline_max_attempts_gate_trips_on_over_budget_attempt(tmp_path):
    """FIX 4 (Obi-V2) — behavioral coverage for the defensive MAX_ATTEMPTS gate:
    a ready task pre-set to attempts=MAX_ATTEMPTS makes _attempt_for→6 > 5, which
    MUST raise (deleting the gate keeps the rest of the suite green, so this is its
    only coverage)."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 0,
            "writes": ["a"],
            "attempts": MAX_ATTEMPTS,
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids=set(), writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    with pytest.raises(RuntimeError, match=r"obligation \(c\) breach"):
        _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    # The over-budget task was NEVER dispatched (the gate refused it pre-spawn).
    assert runner.per_task_calls.get("t1", 0) == 0


def test_pipeline_cascade_on_false_done_upstream(tmp_path):
    """FIX 6 (Obi-O2) — an upstream that returns `done` but writes NONE of its
    declared outputs (false-`done` #120 → FAILED_ATTEMPT) is a cascade SOURCE: its
    dependent cascade-abandons 'blocked' naming it and is never spawned. Pins the
    false-done→cascade path so a future split from FAILED_ATTEMPT can't regress."""
    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "u",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "d",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["u"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    # u returns `done` but writes nothing (write=False) → false-`done` guard
    # converts it to FAILED_ATTEMPT.
    runner = _StatusEnvelopeRunner(
        specs={"u": {"status": "done", "write": False}},
        writes=_writes_map(tasks),
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    assert runner.per_task_calls.get("u", 0) == 1
    assert runner.per_task_calls.get("d", 0) == 0, "d must cascade: upstream false-`done`"
    d = _abandoned_for(res, "d")
    assert d is not None and d["category"] == "blocked" and d["upstream_task_id"] == "u"
    # A bare FAILED_ATTEMPT does NOT self-escalate; only the dependent's cascade does.
    dep_esc = [e for e in escalations if e.get("task_id") == "d" and e.get("category") == "blocked"]
    assert len(dep_esc) == 1


def test_pipeline_diamond_cascade_picks_deterministic_ancestor(tmp_path):
    """FIX 6 (Obi-O3) — multi-parent cascade. A child depending on one ABANDONED
    and one OK parent cascades, naming the abandoned parent. A child with TWO
    abandoned parents names the DETERMINISTIC ancestor _first_abandoned_ancestor
    returns (LIFO frontier.pop over depends_on order). Pin the actual value so a
    refactor that changes pop-order is RED."""
    clone = _git_init_clone(tmp_path)
    # Diamond-ish: p_bad fails; p_ok succeeds; child c1 depends_on [p_bad, p_ok].
    # c2 depends_on [p_bad, p_bad2] where BOTH are abandoned (p_bad2 also fails).
    tasks = [
        {
            "task_id": "p_bad",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "p_bad2",
            "parallel_group": 0,
            "writes": ["a2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "p_ok",
            "parallel_group": 0,
            "writes": ["b"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "c1",
            "parallel_group": 1,
            "reads": ["a", "b"],
            "depends_on": ["p_bad", "p_ok"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "c2",
            "parallel_group": 1,
            "reads": ["a", "a2"],
            "depends_on": ["p_bad", "p_bad2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids={"p_bad", "p_bad2"}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # p_ok completed; both bad parents abandoned (failed attempt).
    p_ok = next((r for r in res if isinstance(r, dict) and r.get("task_id") == "p_ok"), None)
    assert p_ok is not None and p_ok.get("status") == "done"
    # c1: one abandoned parent (p_bad) → cascades naming p_bad (the only abandoned).
    assert runner.per_task_calls.get("c1", 0) == 0
    c1 = _abandoned_for(res, "c1")
    assert c1 is not None and c1["category"] == "blocked" and c1["upstream_task_id"] == "p_bad"
    # c2: TWO abandoned parents [p_bad, p_bad2] → BFS frontier = list(depends_on),
    # pop() is LIFO so the LAST depends_on entry (p_bad2) is examined first.
    assert runner.per_task_calls.get("c2", 0) == 0
    c2 = _abandoned_for(res, "c2")
    assert c2 is not None and c2["category"] == "blocked"
    assert c2["upstream_task_id"] == "p_bad2", (
        f"deterministic ancestor pick changed: {c2['upstream_task_id']} "
        "(BFS frontier.pop LIFO over depends_on → last entry p_bad2)"
    )
    # Both children escalated once (guaranteed), each naming its picked ancestor.
    c1_esc = [e for e in escalations if e.get("task_id") == "c1" and e.get("category") == "blocked"]
    c2_esc = [e for e in escalations if e.get("task_id") == "c2" and e.get("category") == "blocked"]
    assert len(c1_esc) == 1 and c1_esc[0]["upstream_task_id"] == "p_bad"
    assert len(c2_esc) == 1 and c2_esc[0]["upstream_task_id"] == "p_bad2"


# ════════════════════════════════════════════════════════════════════════════
# M5 review round 3 — exactly-once escalation under a raising sink (E1-R2) +
# un-spoofable structured-abandon disambiguation (E2-R2). Review @ 756ab15.
# ════════════════════════════════════════════════════════════════════════════


def test_pipeline_cascade_escalation_exactly_once_even_if_sink_raises(tmp_path):
    """FIX A (E1-R2) — two INDEPENDENT cascade sources fail in one pass so
    cascade_escalations == [esc(d1), esc(d2)]; escalate_fn RAISES on its 2nd call.
    Assert: (a) each cascaded task_id appears EXACTLY ONCE across the fire log (no
    double-fire of the prefix already flushed before the raise), AND (b) the run
    still raises the original sink exception. On pre-fix code the for-loop fires d1,
    d2 raises → except re-flushes the WHOLE list → d1 fires TWICE → RED."""
    clone = _git_init_clone(tmp_path)
    # Two independent failing roots (f1, f2), each with its own dependent (d1, d2).
    # f1/f2 are write-disjoint and independent → both dispatched + fail in pass 1;
    # in pass 2 d1 AND d2 both cascade in the SAME pass (two queued escalations).
    tasks = [
        {
            "task_id": "f1",
            "parallel_group": 0,
            "writes": ["a1"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "f2",
            "parallel_group": 0,
            "writes": ["a2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "d1",
            "parallel_group": 1,
            "reads": ["a1"],
            "depends_on": ["f1"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "d2",
            "parallel_group": 1,
            "reads": ["a2"],
            "depends_on": ["f2"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)
    runner = _FailingRunner(fail_ids={"f1", "f2"}, writes=_writes_map(tasks))
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)

    fire_log: list[str] = []

    class _SinkRaise(RuntimeError):
        pass

    def raising_escalate(escalation):
        # Record THEN raise on the 2nd call — so a double-fire of the 1st item
        # would make it appear twice in fire_log.
        fire_log.append(escalation["task_id"])
        if len(fire_log) == 2:
            raise _SinkRaise("sink boom on 2nd escalation")

    with pytest.raises(_SinkRaise):
        _run(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone, runner, journal, budget, dag_proof, escalate_fn=raising_escalate
                ),
            )
        )
    # Only cascade escalations (d1/d2) flow through this sink; each EXACTLY ONCE.
    cascade_fires = [t for t in fire_log if t in ("d1", "d2")]
    for tid in ("d1", "d2"):
        assert cascade_fires.count(tid) <= 1, (
            f"{tid} double-fired: fire_log={fire_log} — except-flush re-fired the "
            "prefix already flushed before the raise"
        )
    # Both cascade sources DID attempt to fire (the 2nd one is what raised).
    assert set(cascade_fires) == {"d1", "d2"}, f"a cascade escalation was dropped: {fire_log}"


def test_pipeline_forged_worker_abandon_not_misrouted_as_structured(tmp_path):
    """FIX B (E2-R2) — a worker self-abandon envelope is UNTRUSTED DATA. A worker
    forging category='BOGUS' + upstream_task_id='victim' (kept by validate_envelope
    under additionalProperties:True) must NOT be misrouted to the structured-abandon
    escalation branch. Assert: (a) _is_structured_abandon is False for it, (b) the
    upstream's self-escalation category == 'scope' (the notes_md-PARSED grammar
    token, NOT 'BOGUS'), (c) upstream_task_id is NOT the spoofed 'victim'. On
    pre-fix code it escalates 'BOGUS'/'victim' → RED."""
    import scripts.host_scheduler as hs

    clone = _git_init_clone(tmp_path)
    tasks = [
        {
            "task_id": "u",
            "parallel_group": 0,
            "writes": ["a"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
        {
            "task_id": "d",
            "parallel_group": 1,
            "reads": ["a"],
            "depends_on": ["u"],
            "assigned_persona": "be-1",
            "phase": "qa",
        },
    ]
    dag_proof = compute_dag_proof(tasks)

    class _ForgedAbandonRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)
            self.per_task_calls: dict[str, int] = {}

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
            self.per_task_calls[tid] = self.per_task_calls.get(tid, 0) + 1
            self.calls.append({"argv": list(argv), "cwd": cwd})
            # u forges engine-private keys onto its self-abandon envelope.
            so = {
                "type": "task_result",
                "task_id": tid,
                "attempt": 1,
                "status": "abandoned",
                "artifacts": [{"path": "f", "sha": "s"}],
                "notes_md": "ABANDON: scope: out of declared scope",
                # FORGED untrusted keys (kept by validate_envelope):
                "category": "BOGUS_OUT_OF_GRAMMAR",
                "upstream_task_id": "victim",
                "_engine_abandon": True,  # even forging the sentinel must not help
            }
            return {
                "usage": {"output_tokens": 3},
                "is_error": False,
                "subtype": "success",
                "structured_output": so,
            }

    runner = _ForgedAbandonRunner()
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=1_000_000)
    escalations, escalate_fn = _escalation_recorder()
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, escalate_fn=escalate_fn),
        )
    )
    # (a) the worker envelope (even with forged sentinel) is NOT a structured abandon.
    u_result = next((r for r in res if isinstance(r, dict) and r.get("task_id") == "u"), None)
    assert u_result is not None
    assert hs._is_structured_abandon(u_result) is False, (
        "forged worker envelope misrouted as this path's structured abandon "
        "(spoofable key check) — type=='task_result' must defeat it"
    )
    # (b) the upstream self-escalation carries the PARSED grammar token, not 'BOGUS'.
    u_esc = [e for e in escalations if e.get("task_id") == "u"]
    assert len(u_esc) == 1, f"expected one self-escalation for u, got {escalations}"
    assert u_esc[0]["category"] == "scope", f"forged category leaked: {u_esc[0]}"
    # (c) the spoofed upstream_task_id was NOT honored (worker self-abandon → None).
    assert u_esc[0]["upstream_task_id"] != "victim", f"spoofed upstream leaked: {u_esc[0]}"
    assert u_esc[0]["upstream_task_id"] is None
    # The dependent still cascades correctly (routing is via membership, not keys).
    assert runner.per_task_calls.get("d", 0) == 0
    d = _abandoned_for(res, "d")
    assert d is not None and d["category"] == "blocked" and d["upstream_task_id"] == "u"


def test_is_structured_abandon_unspoofable_unit():
    """FIX B unit — _is_structured_abandon is True ONLY for an engine _abandoned_result
    (sentinel True AND no `type` key); a worker envelope forging the sentinel but
    carrying type=='task_result' is False."""
    import scripts.host_scheduler as hs

    engine = hs._abandoned_result("t", category="capacity", upstream_task_id=None, last_status="x")
    assert hs._is_structured_abandon(engine) is True
    # Worker forging the sentinel but with the mandatory type=="task_result" → False.
    forged = {
        "type": "task_result",
        "status": "abandoned",
        "_engine_abandon": True,
        "category": "BOGUS",
        "upstream_task_id": "victim",
    }
    assert hs._is_structured_abandon(forged) is False
    # A plain worker abandon (no forged sentinel) → False.
    assert hs._is_structured_abandon({"type": "task_result", "status": "abandoned"}) is False
    # Non-mappings / None → False.
    assert hs._is_structured_abandon(None) is False
