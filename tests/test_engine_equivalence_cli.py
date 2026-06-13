"""EQUIVALENCE PROOF — drive the REAL WaveDispatcher with the CLI seams.

This is THE proof that swapping the bridge transport for the CLI adapter is
behavior-preserving: we construct the EXISTING ``scripts.pm_dispatch.WaveDispatcher``
(unmodified engine) and inject ``build_cli_spawn_fn`` / ``build_cli_poll_fn`` from
``scripts.cli_dispatch`` over a :class:`FakeCliRunner` (NO real ``claude``), then
assert the engine's existing invariants still hold:

  * happy-path: a 3-wave DAG completes in wave order, each task `complete`,
    attempts charged exactly once;
  * single-re-queue / attempt budget: a worker that always fails (CLI is_error)
    burns exactly MAX_ATTEMPTS then abandons (capacity), with one escalation;
  * terminal-only wave gate: wave N+1 does not start until wave N is terminal;
  * GO-OBSERVE done-but-silent: a worker whose envelope only resolves on the
    confirming re-read at the deadline is captured as success, NOT charged a
    failed attempt.

The engine is SYNCHRONOUS; the CLI futures live on the tools' own loop. We wire
the engine's injected ``sleep_fn`` to ``tools.pump()`` so each no-progress poll
round drains the scheduled ``run_attempt`` coroutines — i.e. the engine itself
drives the async transport, unchanged.

The fixture + helpers (``workspace``, ``FakeClock``, ``_seed_task``,
``_task_row``) are imported from the canonical engine test module so we drive the
SAME real DB-backed engine, not a reimplementation.
"""

from __future__ import annotations

import asyncio

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    CliDispatchTools,
    FakeCliRunner,
    build_cli_poll_fn,
    build_cli_spawn_fn,
)
from scripts.pm_dispatch import MAX_ATTEMPTS, WALL_CLOCK_S, WaveDispatcher
from scripts.result_journal import ResultJournal

# Reuse the canonical engine-test fixture + helpers so we drive the REAL engine.
from tests.test_pm_dispatch import (  # noqa: F401 — `workspace` is a pytest fixture
    FakeClock,
    _seed_task,
    _task_row,
    workspace,
)


def _make_tools(runner, *, loop):
    return CliDispatchTools(
        budget=BudgetPool(total_tokens=10_000_000),
        journal=ResultJournal(),
        clone_dir="/tmp",  # nosec B108 — not opened; the guard resolves it, tests stay inside
        model_for=lambda task, attempt: "sonnet",
        briefing_for=lambda task, attempt: f"briefing for {task['task_id']}",
        runner=runner,
        loop=loop,
    )


def _engine_task_rows(ws, ids):
    """Build the row dicts the engine passes to spawn/poll. The engine reads
    `task["task_id"]` in the CLI seams but `task["id"]` for DB mutation, so we
    mirror `id` into `task_id` (the production host plan emits `task_id`)."""
    rows = []
    for tid in ids:
        row = _task_row(ws, tid)
        row["task_id"] = row["id"]
        rows.append(row)
    return rows


def _run_engine(ws, runner, tasks, *, clock=None, escalate=None):
    """Drive the REAL WaveDispatcher with the CLI seams, pumping the loop via the
    engine's own sleep_fn."""
    loop = asyncio.new_event_loop()
    try:
        tools = _make_tools(runner, loop=loop)
        d = WaveDispatcher(
            ws["db"],
            spawn_fn=build_cli_spawn_fn(tools),
            poll_fn=build_cli_poll_fn(tools),
            escalate_fn=escalate if escalate is not None else (lambda e: None),
            clock=clock if clock is not None else FakeClock(),
            # Each no-progress poll round drains the scheduled run_attempt coros:
            # the engine drives the async transport unchanged.
            sleep_fn=lambda s: tools.pump(),
        )
        summaries = d.run(tasks)
        return summaries, d, tools
    finally:
        loop.close()


# ── happy path: 3 waves, in order, complete ─────────────────────────────────


def test_cli_seams_three_wave_happy_path(workspace):  # noqa: F811
    t0 = _seed_task(workspace, title="w0", parallel_group=0)
    t1 = _seed_task(workspace, title="w1", parallel_group=1)
    t2 = _seed_task(workspace, title="w2", parallel_group=2)

    runner = FakeCliRunner(structured_output=lambda argv, cwd: None)
    # The default fake returns a fixed task_id; we need per-task echo, so derive
    # the envelope from the prompt's task id. Simpler: a callable keyed on argv.
    runner.structured_output = _structured_output_from_argv

    tasks = _engine_task_rows(workspace, (t2, t0, t1))  # shuffled input
    summaries, _d, _ = _run_engine(workspace, runner, tasks)

    assert len(summaries) == 3
    assert all(s["terminal_only"] for s in summaries)
    for tid in (t0, t1, t2):
        row = _task_row(workspace, tid)
        assert row["status"] == "complete"
        assert row["attempts"] == 1
    # Non-vacuity: the engine genuinely drove the CLI runner once per task (the
    # seams are NOT no-ops). Exactly 3 real run_attempt → FakeCliRunner calls.
    assert runner.call_count == 3


def _structured_output_from_argv(argv, cwd):
    """Echo the dispatched task_id/attempt from the -p prompt so the envelope
    validates against the host's dispatch identity. The prompt is
    'Perform task <id> (attempt <n>) ...'."""
    import re

    prompt = argv[argv.index("-p") + 1]
    m = re.search(r"Perform task (\S+) \(attempt (\d+)\)", prompt)
    task_id, attempt = m.group(1), int(m.group(2))
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": "done",
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "done",
        "next_action": "review",
    }


# ── single-re-queue / attempt budget: persistent failure → capacity abandon ──


def test_cli_seams_attempt_budget_exhaustion(workspace):  # noqa: F811
    """A worker whose CLI call always errors (is_error) is a failed attempt each
    round → the engine re-queues through its SINGLE re-queue site exactly
    MAX_ATTEMPTS times, then abandons capacity with one escalation. This proves
    the termination bound holds with the CLI poll seam (a failed attempt surfaces
    as poll_fn → None, the silently-dead-worker path)."""
    tid = _seed_task(workspace, title="always-fail", parallel_group=0)

    # is_error=True on every call → run_attempt returns FAILED_ATTEMPT → the CLI
    # poll seam returns None → engine soft-kills/re-queues each round.
    runner = FakeCliRunner(is_error=True)
    clock = FakeClock()
    escalations = []

    # The worker never produces a terminal envelope; the engine's wall-clock trip
    # drives the soft-kill. Advance the clock each pump so the deadline fires.
    def escalate(e):
        escalations.append(e)

    # We need the deadline to trip: wire a clock that jumps on each pump. Since
    # the failed attempt resolves immediately (poll returns None) but the engine
    # only re-queues on the wall-clock trip, advance the clock via sleep_fn.
    loop = asyncio.new_event_loop()
    try:
        tools = _make_tools(runner, loop=loop)

        def sleep_fn(_s):
            tools.pump()
            clock.advance(WALL_CLOCK_S + 1.0)

        d = WaveDispatcher(
            workspace["db"],
            spawn_fn=build_cli_spawn_fn(tools),
            poll_fn=build_cli_poll_fn(tools),
            escalate_fn=escalate,
            clock=clock,
            sleep_fn=sleep_fn,
        )
        tasks = _engine_task_rows(workspace, (tid,))
        d.run(tasks)
    finally:
        loop.close()

    row = _task_row(workspace, tid)
    assert row["status"] == "abandoned"
    assert row["abandon_category"] == "capacity"
    assert row["attempts"] == MAX_ATTEMPTS
    assert len(escalations) == 1


# ── terminal-only wave gate: wave 2 waits for wave 1 ────────────────────────


def test_cli_seams_wave_gate_blocks_until_terminal(workspace):  # noqa: F811
    """Wave-2 is not dispatched until wave-1 reaches a terminal envelope. We
    record spawn order via a runner that tags which task it saw first."""
    t_w1 = _seed_task(workspace, title="w1", parallel_group=0)
    t_w2 = _seed_task(workspace, title="w2", parallel_group=1)

    spawn_seen = []

    def structured(argv, cwd):
        env = _structured_output_from_argv(argv, cwd)
        spawn_seen.append(env["task_id"])
        return env

    runner = FakeCliRunner(structured_output=structured)
    tasks = _engine_task_rows(workspace, (t_w1, t_w2))
    summaries, _, _ = _run_engine(workspace, runner, tasks)

    # The wave-1 task's CLI call happened before the wave-2 task's.
    assert spawn_seen.index(str(t_w1)) < spawn_seen.index(str(t_w2))
    assert len(summaries) == 2
    assert all(s["terminal_only"] for s in summaries)
    assert _task_row(workspace, t_w1)["status"] == "complete"
    assert _task_row(workspace, t_w2)["status"] == "complete"


# ── GO-OBSERVE done-but-silent captured as success via confirming re-read ───


def test_cli_seams_done_but_silent_go_observe(workspace):  # noqa: F811
    """A worker whose future is not yet `done()` on the in-flight scan (deadline
    trips) but resolves on the confirming re-read is captured as SUCCESS, charged
    exactly one attempt, zero escalations. This pins the engine's GO-OBSERVE gate
    over the CLI future-poll seam.

    Construction: the runner sleeps briefly; the in-flight scan sees a pending
    future (poll → None) and the clock is advanced past WALL_CLOCK_S, so the
    deadline trips; the sleep_fn pump then resolves the future, and the engine's
    `_observe_before_kill` confirming re-read reads the now-done envelope."""
    tid = _seed_task(workspace, title="done-but-silent", parallel_group=0)

    runner = FakeCliRunner(structured_output=_structured_output_from_argv, sleep=0.0)
    clock = FakeClock()
    escalations = []

    loop = asyncio.new_event_loop()
    try:
        tools = _make_tools(runner, loop=loop)

        # First poll round: the future is still pending (not pumped yet) → None.
        # Advance the clock so the deadline trips on this round; THEN the engine
        # calls _observe_before_kill which re-polls — but the future is still
        # pending until we pump. We pump inside sleep_fn, which the engine calls
        # only when in_flight and not progressed. To make the confirming re-read
        # succeed we pump BEFORE the deadline check on the second visit: emulate
        # by advancing the clock once and pumping in sleep_fn.
        state = {"advanced": False}

        def sleep_fn(_s):
            tools.pump()  # resolve the future so the NEXT poll/confirming-read sees it

        # We must trip the deadline. The engine advances no clock itself; do it
        # via a poll-driven hook: wrap the poll_fn to advance the clock on the
        # FIRST None then let the confirming read (after pump) succeed.
        base_poll = build_cli_poll_fn(tools)

        def poll_fn(task, attempt):
            res = base_poll(task, attempt)
            if res is None and not state["advanced"]:
                # In-flight scan saw a pending future → trip the deadline so the
                # engine performs its confirming re-read; pump first so the
                # confirming read (the engine's immediate next poll) resolves.
                clock.advance(WALL_CLOCK_S + 1.0)
                state["advanced"] = True
                tools.pump()
            return res

        d = WaveDispatcher(
            workspace["db"],
            spawn_fn=build_cli_spawn_fn(tools),
            poll_fn=poll_fn,
            escalate_fn=lambda e: escalations.append(e),
            clock=clock,
            sleep_fn=sleep_fn,
        )
        tasks = _engine_task_rows(workspace, (tid,))
        d.run(tasks)
    finally:
        loop.close()

    row = _task_row(workspace, tid)
    assert row["status"] == "complete"
    assert row["attempts"] == 1  # charged ONCE at dispatch, NOT re-dispatched
    assert escalations == []
