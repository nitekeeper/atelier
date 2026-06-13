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
spawned.  The existing suite stays green; ``ATELIER_TRANSPORT`` default
``bridge`` is untouched (the scheduler is reachable only on the CLI path).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.budget_pool import BudgetExceeded, BudgetPool
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


def _run(coro):
    return asyncio.run(coro)


class _TimedRunner(FakeCliRunner):
    """A FakeCliRunner that records per-task START/END instants (monotonic) and
    sleeps a per-task amount, returning a valid envelope keyed to the task.

    Inherits the FAIL-CLOSED fake markers from FakeCliRunner — no real process,
    so the sandbox gate stays exempt."""

    def __init__(self, sleeps: dict[str, float] | None = None):
        super().__init__(structured_output=None)
        self.sleeps = sleeps or {}
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
    runner = _TimedRunner()
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
    runner = _TimedRunner(sleeps={"fast": 0.0, "slow": 0.3})
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
    runner = _TimedRunner(sleeps={"t1": 0.1, "t2": 0.2, "t3": 0.0})
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
    runner = _TimedRunner(sleeps={"t1": 0.2, "t2": 0.2})
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
    runner = _TimedRunner(sleeps={"t1": 0.15, "t2": 0.15})
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
    runner = _TimedRunner(sleeps={"t1": 0.15, "t2": 0.15})
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


# ── budget exhaustion surfaces (terminal, not swallowed) ────────────────────


def test_pipeline_budget_exceeded_propagates(tmp_path):
    """A BudgetExceeded inside a worker is surfaced by pipeline() (terminal) and
    does NOT hang the admission loop."""
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
    # Ceiling 700 < est 6_000 → assert_can_dispatch raises BudgetExceeded pre-spawn.
    budget = BudgetPool(total_tokens=1_000)
    with pytest.raises(BudgetExceeded):
        _run(pipeline(tasks, **_pipeline_kwargs(clone, runner, journal, budget, dag_proof)))
    # The worker never spawned (refused at the budget gate).
    assert runner.call_count == 0


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

    runner1 = _TimedRunner()
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

    class _WriteRunner(FakeCliRunner):
        def __init__(self):
            super().__init__(structured_output=None)

        async def __call__(self, argv, cwd):
            tid = _tid_from_argv(argv)
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
                return {
                    "usage": {"output_tokens": 1},
                    "is_error": True,
                    "subtype": "error",
                    "structured_output": None,
                }
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
