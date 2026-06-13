"""Deterministic host — builds the 3-task DAG and drives the scheduler.

The 3-task DAG
--------------
::

    T1 (writes: a.txt)   T2 (writes: b.txt)    ← write-disjoint → pipeline-able
            \\               /
             └─► T3 (reads: a.txt, b.txt; writes: c.txt)  ← depends_on [T1, T2]

T1 and T2 are independent (different writes, no shared reads) — the scheduler
dispatches them concurrently and does NOT wait for both before advancing T1 to
terminal.  T3 depends on both → genuine barrier (cannot start until both
T1 and T2 are terminal).

Scheduler behaviour (barrier-free pipeline on independent edges)
----------------------------------------------------------------
1. T1 and T2 are launched concurrently (semaphore(2) allows both).
2. The scheduler tracks per-task done timestamps.  T1 reaches TERMINAL state
   (and its done timestamp is recorded) while T2 may still be running — the
   host does NOT block on T2 before declaring T1 complete.
3. T3's readiness is evaluated *continuously*: ``ready(t3) = T1 TERMINAL and T2
   TERMINAL``.  The moment the second of {T1, T2} finishes, T3 is dispatched.
4. Wall-clock ≈ max(T1_duration, T2_duration) + T3_duration, not the sum.

``run_dag`` API
---------------
``run_dag(tasks, *, budget, journal, fake_agent)`` is the public coroutine.  It:
* Builds task dicts from the ``tasks`` list (which carry briefing + model).
* Runs the barrier-free scheduler.
* Returns ``DagResult(envelopes, done_timestamps, abandoned_tasks, query_count)``.

The scheduler is intentionally simple (~80 lines) because M0 is a PoC.  M4
will lift the ``pipeline()``/``parallel()`` façades as a full DAG scheduler.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from spikes.sdk_host_poc.agent_call import run_attempt
from spikes.sdk_host_poc.budget import BudgetPool
from spikes.sdk_host_poc.fake_agent import FakeAgent
from spikes.sdk_host_poc.journal import ResultJournal

# ── Task representation ────────────────────────────────────────────────────────


def make_task(
    task_id: str,
    *,
    persona: str = "backend-engineer-1",
    phase: str = "implement",
    depends_on: list[str] | None = None,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
    briefing: str = "",
    model: str = "claude-sonnet-4-5",
) -> dict[str, Any]:
    """Construct a minimal task dict for the spike DAG."""
    return {
        "task_id": task_id,
        "assigned_persona": persona,
        "phase": phase,
        "depends_on": depends_on or [],
        "reads": reads or [],
        "writes": writes or [],
        "briefing": briefing or f"Task {task_id}: implement your assigned work.",
        "model": model,
    }


def build_poc_tasks(
    *,
    t1_briefing: str = "",
    t2_briefing: str = "",
    t3_briefing: str = "",
    model: str = "claude-sonnet-4-5",
) -> list[dict[str, Any]]:
    """Build the canonical 3-task PoC DAG.

    Accepts optional briefing overrides so tests can mutate T1's input and
    trigger journal invalidation (A5).
    """
    t1 = make_task(
        "t1",
        writes=["a.txt"],
        briefing=t1_briefing or "T1: write a.txt",
        model=model,
    )
    t2 = make_task(
        "t2",
        writes=["b.txt"],
        briefing=t2_briefing or "T2: write b.txt",
        model=model,
    )
    t3 = make_task(
        "t3",
        depends_on=["t1", "t2"],
        reads=["a.txt", "b.txt"],
        writes=["c.txt"],
        briefing=t3_briefing or "T3: read a.txt and b.txt, write c.txt",
        model=model,
    )
    return [t1, t2, t3]


# ── DAG result ─────────────────────────────────────────────────────────────────


@dataclass
class DagResult:
    """Return value of ``run_dag``."""

    # Per-task validated envelopes (task_id → envelope dict or None if abandoned)
    envelopes: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    # Per-task START timestamps (task_id → monotonic time; set the instant the
    # task's dispatch coroutine begins, BEFORE the query runs).  This is the
    # load-bearing telemetry for the A4 barrier proof: start[t3] must be
    # >= max(done[t1], done[t2]) regardless of any sleep-ratio fixture choice.
    start_timestamps: dict[str, float] = field(default_factory=dict)
    # Per-task done timestamps (task_id → monotonic time; set when terminal)
    done_timestamps: dict[str, float] = field(default_factory=dict)
    # task_ids that were abandoned (BudgetExceeded or other terminal failure)
    abandoned_tasks: list[str] = field(default_factory=list)
    # Total query() calls recorded by the fake
    query_count: int = 0
    # Per-task usage dicts (task_id → usage)
    usages: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Exceptions raised per task (task_id → exception or None)
    exceptions: dict[str, Exception | None] = field(default_factory=dict)


# ── Scheduler internals ────────────────────────────────────────────────────────


async def _run_one_task(
    task: dict[str, Any],
    *,
    budget: BudgetPool,
    journal: ResultJournal,
    fake_agent: FakeAgent,
    upstream_envelope_hashes: list[str],
    est_per_agent: int = 500,
) -> tuple[dict[str, Any] | None, Exception | None]:
    """Run a single task attempt via ``run_attempt``.

    Returns ``(envelope, None)`` on success or ``(None, exception)`` on failure.
    The exception is never re-raised here — the scheduler decides routing.
    """
    task_id = str(task["task_id"])
    attempt = 1
    try:
        envelope = await run_attempt(
            task,
            attempt,
            budget=budget,
            journal=journal,
            model=task["model"],
            briefing=task["briefing"],
            query_fn=fake_agent.query_fn(task_id, attempt),
            upstream_envelope_hashes=upstream_envelope_hashes,
            est_per_agent=est_per_agent,
        )
        return envelope, None
    except Exception as exc:
        return None, exc


# ── Public API ─────────────────────────────────────────────────────────────────


async def run_dag(
    tasks: list[dict[str, Any]],
    *,
    budget: BudgetPool,
    journal: ResultJournal,
    fake_agent: FakeAgent,
    est_per_agent: int = 500,
) -> DagResult:
    """Run the 3-task DAG with barrier-free pipeline on independent edges.

    Scheduling algorithm
    --------------------
    1. Index tasks and their dependency sets.
    2. Build a concurrency semaphore (size = number of tasks, effectively
       unlimited for a 3-task DAG — the real M4 uses ``asyncio.Semaphore(2)``).
    3. Launch all tasks whose dependencies are already satisfied concurrently
       via ``asyncio.create_task``.
    4. Wait for tasks to complete; as each finishes record its done timestamp
       and check which blocked tasks are now ready.
    5. A task that raises ``BudgetExceeded`` is abandoned; its downstream
       dependents are also cascade-abandoned (never dispatched).

    Parameters
    ----------
    tasks:
        Task dicts (from ``build_poc_tasks``).
    budget:
        Shared ``BudgetPool``.
    journal:
        ``ResultJournal`` for lookup + persistence.
    fake_agent:
        ``FakeAgent`` instance (or compatible) providing the ``query_fn`` seam.
    est_per_agent:
        Per-agent token estimate for budget preflight.

    Returns
    -------
    DagResult
    """
    # Index tasks
    task_by_id: dict[str, dict[str, Any]] = {str(t["task_id"]): t for t in tasks}
    deps_by_id: dict[str, set[str]] = {
        str(t["task_id"]): {str(d) for d in t.get("depends_on", [])} for t in tasks
    }
    # Downstream: for each task, which tasks depend on it?
    dependents_by_id: dict[str, list[str]] = {tid: [] for tid in task_by_id}
    for tid, deps in deps_by_id.items():
        for dep in deps:
            dependents_by_id.setdefault(dep, []).append(tid)

    result = DagResult()
    terminal: set[str] = set()  # task_ids that have reached a terminal state
    abandoned: set[str] = set()  # task_ids abandoned (budget / cascade)
    envelopes: dict[str, dict[str, Any]] = {}  # task_id → validated envelope
    semaphore = asyncio.Semaphore(len(tasks))  # effectively unlimited for PoC

    def _get_upstream_hashes(task_id: str) -> list[str]:
        """Collect envelope hashes of all transitive upstream dependencies."""
        hashes = []
        for dep_id in deps_by_id.get(task_id, set()):
            if dep_id in envelopes:
                hashes.append(journal.envelope_hash(envelopes[dep_id]))
        return hashes

    def _is_ready(task_id: str) -> bool:
        """True iff all dependencies are terminal and none are abandoned."""
        deps = deps_by_id.get(task_id, set())
        if not deps:
            return True
        # All deps must be terminal (done or failed) and NOT in abandoned
        # (abandoned deps cascade-abandon this task; we check that separately)
        return deps.issubset(terminal) and not deps.intersection(abandoned)

    def _cascade_abandon(task_id: str) -> None:
        """Abandon *task_id* and all its transitive dependents."""
        if task_id in abandoned:
            return
        abandoned.add(task_id)
        terminal.add(task_id)
        result.envelopes[task_id] = None
        result.abandoned_tasks.append(task_id)
        # Record done timestamp even for abandoned tasks
        if task_id not in result.done_timestamps:
            result.done_timestamps[task_id] = time.monotonic()
        for dep_task_id in dependents_by_id.get(task_id, []):
            _cascade_abandon(dep_task_id)

    async def _dispatch(task_id: str) -> None:
        """Dispatch one task under the semaphore."""
        # Record START timestamp the instant this task's dispatch begins — the
        # scheduler only launches _dispatch after _is_ready() is True, so this
        # captures "when the host decided this task could start".  It is the
        # load-bearing telemetry for the A4 barrier proof (start[t3] must be
        # >= max(done[t1], done[t2]) — independent of any sleep-ratio fixture).
        result.start_timestamps[task_id] = time.monotonic()
        async with semaphore:
            task = task_by_id[task_id]
            upstream_hashes = _get_upstream_hashes(task_id)
            envelope, exc = await _run_one_task(
                task,
                budget=budget,
                journal=journal,
                fake_agent=fake_agent,
                upstream_envelope_hashes=upstream_hashes,
                est_per_agent=est_per_agent,
            )
            ts = time.monotonic()
            if exc is not None:
                # Route failure — cascade-abandon (BudgetExceeded or other)
                _cascade_abandon(task_id)
                result.exceptions[task_id] = exc
            else:
                envelopes[task_id] = envelope
                terminal.add(task_id)
                result.done_timestamps[task_id] = ts
                result.envelopes[task_id] = envelope
                result.exceptions[task_id] = None

    # ── Event-loop style scheduler ─────────────────────────────────────────
    # We run a cooperative loop: dispatch all ready tasks, collect completions,
    # repeat until all tasks are terminal.

    pending_dispatch: set[str] = set(task_by_id.keys())
    active_tasks: dict[str, asyncio.Task] = {}

    while pending_dispatch or active_tasks:
        # Launch all tasks whose deps are satisfied and aren't already running
        for tid in list(pending_dispatch):
            if tid in abandoned:
                pending_dispatch.discard(tid)
                continue
            if _is_ready(tid):
                pending_dispatch.discard(tid)
                t = asyncio.create_task(_dispatch(tid))
                active_tasks[tid] = t

        if not active_tasks:
            # Deadlock guard: no tasks running and some still pending
            # → must be abandoned / nothing can run
            for tid in list(pending_dispatch):
                _cascade_abandon(tid)
            pending_dispatch.clear()
            break

        # Wait for at least one active task to finish
        done_set, _ = await asyncio.wait(
            list(active_tasks.values()), return_when=asyncio.FIRST_COMPLETED
        )

        # Collect finished tasks
        finished_tids = [tid for tid, t in active_tasks.items() if t in done_set]
        for tid in finished_tids:
            del active_tasks[tid]
            # Check if any tasks that depended on this are now blocked by abandon
            for dep_tid in dependents_by_id.get(tid, []):
                if dep_tid not in terminal and dep_tid not in abandoned:
                    deps = deps_by_id.get(dep_tid, set())
                    if deps.intersection(abandoned):
                        # A dep was abandoned → cascade-abandon this one too
                        pending_dispatch.discard(dep_tid)
                        _cascade_abandon(dep_tid)

    # Populate query count from the fake agent
    result.query_count = fake_agent.call_count

    return result
