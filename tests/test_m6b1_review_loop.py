"""M6b-1 — the host REVIEW-FIX LOOP safety suite (the delicate-concurrency half).

These Iron-Law tests are LOAD-BEARING and built to be UN-FAKEABLE. They drive the
REAL :func:`scripts.host_scheduler.pipeline` / ``run_host_pipeline_for_project``
through M3's :class:`~scripts.cli_dispatch.FakeCliRunner` (subclassed) — NO real
``claude`` is spawned, and the FakeCliRunner is exempt from the mandatory-sandbox
gate.

Covered (un-fakeable):

* a reviewer BLOCK verdict re-dispatches the IMPLEMENT exactly once (attempts
  incremented), the reviewer re-runs, and the pair ends a clean success;
* the loop respects the ≤MAX_ATTEMPTS bound, then abandons category=capacity +
  escalates exactly once (no infinite loop / no defensive-gate breach);
* the dispatch-time reviewer-disjointness re-check rejects a same-persona reviewer
  (EXACT-STRING, parity with planner) — routed through the finalize tail so the
  loop never hangs;
* the in-memory pairing is available at host dispatch from run_plan_phase output;
* the sub-procedure charges a CHILD BudgetPool(parent=) whose charges BUBBLE to
  the parent, AND reuses the SAME single Semaphore (probed by max concurrency
  staying under the SINGLE global cap, never 2x);
* a reviewer ABANDON (worker failure, NOT a BLOCK verdict) does NOT re-dispatch
  the implement, and its dependent cascades blocked exactly once — with NO
  duplicate self-escalation and the two categories kept distinct;
* an empty/None pairing leaves pipeline behavior byte-identical (no-op).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import threading
from pathlib import Path

import pytest

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import FakeCliRunner, is_failed_attempt
from scripts.dag import compute_dag_proof
from scripts.host_scheduler import (
    pipeline,
    run_host_pipeline_for_project,
    simple_worktree_factory,
)
from scripts.pm_dispatch import MAX_ATTEMPTS
from scripts.result_journal import ResultJournal

# Parse the dispatched task_id + attempt out of the `-p` prompt run_attempt builds.
_PROMPT_RE = re.compile(r"Perform task (\S+) \(attempt (\d+)\)")


def _val(argv, flag):
    return argv[argv.index(flag) + 1]


def _tid_attempt(argv) -> tuple[str, int]:
    m = _PROMPT_RE.search(_val(argv, "-p"))
    assert m is not None, f"unexpected prompt: {_val(argv, '-p')!r}"
    return m.group(1), int(m.group(2))


def _run(coro):
    return asyncio.run(coro)


def _run_bounded(coro, timeout=10.0):
    """Run *coro* under a HARD wall-clock timeout so a non-terminating review-fix
    loop (e.g. a mutation that never advances the implement attempt → the
    MAX_ATTEMPTS bound never trips) fails as a DETERMINISTIC TimeoutError with a
    clear message, rather than hanging the whole CI run and masking the originating
    test.

    Runs the coroutine on its OWN event loop in a DAEMON thread and joins with the
    timeout. A daemon thread is mandatory here (not ``asyncio.wait_for``): a hung
    ``pipeline`` absorbs cooperative cancellation in its own except/cleanup
    ``asyncio.gather(*all_workers)`` drain (it awaits a worker that never
    terminates), so ``wait_for``'s CancelledError would itself be swallowed and the
    test would still hang. The daemon thread is abandoned on timeout (it dies with
    the test process), and the test fails LOUDLY here instead.
    """
    box: dict = {}

    def _worker():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # surface the real failure to the main thread
            box["error"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(
            f"pipeline did not terminate within {timeout}s — a non-terminating "
            "review-fix loop (the MAX_ATTEMPTS bound is not enforced / the implement "
            "attempt never advances). Failing deterministically instead of hanging CI."
        )
    if "error" in box:
        raise box["error"]
    return box["result"]


def _git_init_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir(parents=True, exist_ok=True)
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


def _env(task_id, attempt, status="done", *, artifacts=True, notes="ok"):
    e = {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "artifacts": [{"path": "f.py", "sha": "s"}] if artifacts else [],
        "notes_md": notes,
    }
    return e


class _VerdictRunner(FakeCliRunner):
    """A FakeCliRunner whose envelope is decided by a per-(task_id) verdict policy.

    * ``verdicts``: ``{task_id: callable(attempt) -> envelope|"FAILED"}``. A
      missing task_id defaults to an honest ``done`` writer. Returning the string
      ``"FAILED"`` makes the runner emit an ``is_error`` result → run_attempt
      yields FAILED_ATTEMPT (a WORKER FAILURE, not a verdict).
    * Honest writer: CREATEs each declared output in the task's cwd so the engine's
      false-`done` guard is satisfied (existence is the contract). Only on a `done`
      envelope (a non-done verdict produced no real output, which is correct).
    * Records per-task dispatch counts + the max observed concurrency (semaphore
      probe — must never exceed the SINGLE global cap, not 2x).
    """

    def __init__(self, *, verdicts=None, writes=None, delay=0.0):
        super().__init__(structured_output=None)
        self.verdicts = verdicts or {}
        self.writes = writes or {}
        # A real SUSPENSION delay held INSIDE the instrumented critical section
        # (between _concurrency++ and _concurrency--) so two runner bodies actually
        # OVERLAP on the loop when admitted concurrently — without it, the runner
        # runs to completion synchronously and `max_concurrency` could never observe
        # 2 regardless of how many semaphores guarded the dispatch (the probe would
        # be a tautology). Mirrors tests/test_host_scheduler.py:125-145.
        self.delay = delay
        self.dispatch_count: dict[str, int] = {}
        self.dispatches: list[tuple[str, int]] = []
        self._concurrency = 0
        self.max_concurrency = 0
        self._lock = asyncio.Lock()

    async def __call__(self, argv, cwd):
        tid, attempt = _tid_attempt(argv)
        async with self._lock:
            self.dispatch_count[tid] = self.dispatch_count.get(tid, 0) + 1
            self.dispatches.append((tid, attempt))
            self._concurrency += 1
            self.max_concurrency = max(self.max_concurrency, self._concurrency)
        # SUSPENSION POINT inside the critical section — the un-fakeable overlap
        # window. A 2nd semaphore in the fix loop would let an inner dispatch enter
        # here while an outer one is still suspended → max_concurrency observes 2.
        await asyncio.sleep(self.delay)
        try:
            policy = self.verdicts.get(tid)
            env = policy(attempt) if policy is not None else _env(tid, attempt)
            if env == "FAILED":
                self.calls.append({"argv": list(argv), "cwd": cwd})
                return {
                    "usage": {"output_tokens": 5},
                    "total_cost_usd": 0.0,
                    "is_error": True,
                    "subtype": "error",
                    "session_id": "s",
                    "num_turns": 1,
                    "stop_reason": "end_turn",
                    "structured_output": None,
                }
            # Honest writer: only a `done` envelope created real output.
            if env.get("status") == "done":
                for rel in self.writes.get(tid, []):
                    p = Path(cwd) / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(f"by {tid} attempt {attempt}")
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 5},
                "total_cost_usd": 0.0,
                "is_error": False,
                "subtype": "success",
                "session_id": "s",
                "num_turns": 1,
                "stop_reason": "end_turn",
                "structured_output": env,
            }
        finally:
            async with self._lock:
                self._concurrency -= 1


def _impl_review_tasks(*, reviewer_persona="code-reviewer-1"):
    """A 2-task pairing: IMPL writes a.py (backend-engineer-1); REVIEW reviews it
    (reviewer_persona, depends_on+reads a.py). The reviewer has no writes (read-only
    → no worktree, exempt from the false-`done` guard)."""
    return [
        {
            "task_id": "IMPL",
            "parallel_group": 1,
            "assigned_persona": "backend-engineer-1",
            "depends_on": [],
            "reads": [],
            "writes": ["a.py"],
            "description": "implement foo",
        },
        {
            "task_id": "REVIEW",
            "parallel_group": 2,
            "assigned_persona": reviewer_persona,
            "depends_on": ["IMPL"],
            "reviews": "IMPL",
            "reads": ["a.py"],
            "writes": [],
            "description": "review foo",
        },
    ]


def _pipeline_kwargs(clone, runner, journal, budget, dag_proof, **over):
    base = {
        "budget": budget,
        "journal": journal,
        "dag_proof": dag_proof,
        "model_for": lambda t, a: "sonnet",
        "briefing_for": lambda t, a: "b",
        "clone_dir": str(clone),
        "runner": runner,
        "worktree_factory": simple_worktree_factory(clone),
    }
    base.update(over)
    return base


# ── Iron-Law 1: BLOCK → PASS re-dispatches the implement exactly once ─────────


def test_host_review_fix_loop_block_then_pass_redispatches_implement(tmp_path):
    """Reviewer BLOCKs attempt 1, PASSes after the implement re-dispatch. Assert the
    implement re-dispatches exactly once with attempt INCREMENTED, the reviewer
    re-runs, and the final state is a clean success for BOTH tasks.

    RED pre-fix: pipeline dispatches each task once, no loop → IMPL dispatch count
    == 1 (no re-dispatch), and there is no `review_pairing` kwarg at all.
    """
    clone = _git_init_clone(tmp_path)
    tasks = _impl_review_tasks()
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    # Reviewer: BLOCK on its 1st run, PASS on its 2nd.
    review_runs = {"n": 0}

    def review_policy(attempt):
        review_runs["n"] += 1
        return _env("REVIEW", attempt, status="done" if review_runs["n"] >= 2 else "blocked")

    runner = _VerdictRunner(
        verdicts={"REVIEW": review_policy},
        writes={"IMPL": ["a.py"]},
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"IMPL": "REVIEW", "REVIEW": "IMPL"}
    # Bounded: a mutation that fails to advance the implement attempt would loop
    # forever — fail as a clean TimeoutError, not a CI hang.
    res = _run_bounded(
        pipeline(
            tasks,
            **_pipeline_kwargs(clone, runner, journal, budget, dag_proof, review_pairing=pairing),
        )
    )
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    # Both terminal success.
    assert by_id["IMPL"]["status"] == "done"
    assert by_id["REVIEW"]["status"] == "done"
    # IMPL dispatched EXACTLY twice (initial + one fix), with attempts 1 then 2.
    assert runner.dispatch_count["IMPL"] == 2, runner.dispatches
    impl_attempts = sorted(a for (t, a) in runner.dispatches if t == "IMPL")
    assert impl_attempts == [1, 2], runner.dispatches
    # REVIEW ran twice (block, then pass).
    assert runner.dispatch_count["REVIEW"] == 2, runner.dispatches


# ── Iron-Law 2: the MAX_ATTEMPTS bound holds, then abandon+escalate once ──────


def test_host_review_loop_respects_max_attempts_bound(tmp_path):
    """Reviewer ALWAYS BLOCKs. Assert the implement dispatches at most MAX_ATTEMPTS
    times, then the implement is abandoned (category=capacity) and escalated exactly
    once (no infinite loop, no defensive-gate breach).

    RED if the loop ignores the bound (infinite loop / RuntimeError from the
    defensive MAX_ATTEMPTS gate) or fails to abandon+escalate.
    """
    clone = _git_init_clone(tmp_path)
    tasks = _impl_review_tasks()
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    runner = _VerdictRunner(
        verdicts={"REVIEW": lambda a: _env("REVIEW", a, status="blocked")},
        writes={"IMPL": ["a.py"]},
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"IMPL": "REVIEW", "REVIEW": "IMPL"}
    escalations: list[dict] = []
    # Bounded: if the MAX_ATTEMPTS bound is ever NOT enforced (mutation: attempt not
    # advanced), the loop spins forever — fail as a deterministic TimeoutError here
    # rather than hanging CI and masking the originating test.
    res = _run_bounded(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                review_pairing=pairing,
                escalate_fn=escalations.append,
            ),
        )
    )
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    # IMPL dispatched at most MAX_ATTEMPTS times (the bound holds).
    assert runner.dispatch_count["IMPL"] <= MAX_ATTEMPTS, runner.dispatches
    assert runner.dispatch_count["IMPL"] == MAX_ATTEMPTS, runner.dispatches
    # IMPL abandoned category=capacity.
    assert by_id["IMPL"]["status"] == "abandoned"
    assert by_id["IMPL"]["category"] == "capacity"
    # Exactly ONE capacity escalation for IMPL.
    impl_cap = [
        e for e in escalations if e.get("task_id") == "IMPL" and e.get("category") == "capacity"
    ]
    assert len(impl_cap) == 1, escalations


# ── Iron-Law 3: dispatch-time disjointness rejects a same-persona reviewer ────


def test_dispatch_time_disjointness_rejects_same_persona_reviewer(tmp_path):
    """A reviewer assigned the SAME persona as the implement it reviews (fed
    directly, bypassing synthesis) must be rejected at DISPATCH time by the
    re-check (EXACT-STRING, parity with planner), routed through the finalize tail
    so the loop never hangs.

    RED pre-fix: no dispatch-time check exists → the reviewer dispatches normally.
    """
    clone = _git_init_clone(tmp_path)
    # Reviewer persona == implementer persona → must be rejected.
    tasks = _impl_review_tasks(reviewer_persona="backend-engineer-1")
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    runner = _VerdictRunner(writes={"IMPL": ["a.py"]})
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"IMPL": "REVIEW", "REVIEW": "IMPL"}
    # The disjointness raise propagates out of the worker coroutine and is surfaced
    # by pipeline's post-loop drain — and it is the EXACT planner message.
    with pytest.raises(Exception) as exc:
        _run(
            pipeline(
                tasks,
                **_pipeline_kwargs(
                    clone, runner, journal, budget, dag_proof, review_pairing=pairing
                ),
            )
        )
    msg = str(exc.value)
    assert "reviewer-disjointness" in msg, msg
    assert "backend-engineer-1" in msg, msg
    # The reviewer worker was NEVER dispatched (rejected before run_attempt).
    assert runner.dispatch_count.get("REVIEW", 0) == 0, runner.dispatches


# ── Iron-Law 4: pairing available at host dispatch from in-memory tasks ───────


def test_review_pairing_available_at_host_dispatch_from_in_memory_tasks(tmp_path):
    """run_plan_phase output (in-memory tasks carrying `reviews`) →
    run_host_pipeline_for_project derives a non-empty pairing and the loop ACTIVATES
    (reviewer re-runs after a BLOCK). Drives the REAL host entrypoint.

    RED pre-fix: build_review_pairing does not exist / no review_pairing kwarg → no
    loop → IMPL dispatched once.
    """
    from scripts.host_plan import run_plan_phase

    tasks = _impl_review_tasks()

    def synth(error=None):
        import json

        return "```json\n" + json.dumps(tasks) + "\n```"

    planned, _proof = run_plan_phase(synth, existing_files=set())
    # `reviews` survived parsing into the in-memory task list (the host's sole source).
    assert any(t.get("reviews") == "IMPL" for t in planned), planned

    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True)
    # We need a real git clone for the writer worktree path.
    real_clone = _git_init_clone(tmp_path)
    review_runs = {"n": 0}

    def review_policy(attempt):
        review_runs["n"] += 1
        return _env("REVIEW", attempt, status="done" if review_runs["n"] >= 2 else "blocked")

    runner = _VerdictRunner(verdicts={"REVIEW": review_policy}, writes={"IMPL": ["a.py"]})
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    res = _run(
        run_host_pipeline_for_project(
            planned,
            clone_dir=str(real_clone),
            budget=budget,
            journal=journal,
            runner=runner,
            env={},
            worktree_factory=simple_worktree_factory(real_clone),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
        )
    )
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    assert by_id["IMPL"]["status"] == "done"
    assert by_id["REVIEW"]["status"] == "done"
    # The pairing ACTIVATED: IMPL was re-dispatched after the reviewer's BLOCK.
    assert runner.dispatch_count["IMPL"] == 2, runner.dispatches
    assert runner.dispatch_count["REVIEW"] == 2, runner.dispatches


def test_build_review_pairing_maps_both_ways():
    """The pairing helper maps implement→review AND review→implement; a list with no
    review tasks yields {}."""
    from scripts.planner import build_review_pairing

    tasks = _impl_review_tasks()
    p = build_review_pairing([dict(t) for t in tasks])
    assert p == {"IMPL": "REVIEW", "REVIEW": "IMPL"}, p
    assert build_review_pairing([{"task_id": "x", "parallel_group": 1}]) == {}


# ── Iron-Law 5: child budget bubbles to parent + shares the single semaphore ──


def test_review_loop_child_budget_bubbles_to_parent_and_shares_semaphore(tmp_path):
    """The sub-procedure charges a CHILD BudgetPool(parent=budget) — assert the
    PARENT's spent()/usage_breakdown reflects the child charges (bubbling) — AND the
    sub-procedure reuses the SAME single Semaphore (probed: the max observed
    concurrency stays within the SINGLE global cap, never 2x).

    RED for any impl that forks a fresh (non-parented) pool OR a second semaphore.
    """
    clone = _git_init_clone(tmp_path)
    # Two independent implement+review pairs in the same wave, so concurrency is
    # exercised; each reviewer BLOCKs once then PASSes (drives the child loop).
    tasks = [
        {
            "task_id": "I1",
            "parallel_group": 1,
            "assigned_persona": "backend-engineer-1",
            "writes": ["a.py"],
            "depends_on": [],
            "reads": [],
            "description": "impl 1",
        },
        {
            "task_id": "R1",
            "parallel_group": 2,
            "assigned_persona": "code-reviewer-1",
            "reviews": "I1",
            "depends_on": ["I1"],
            "reads": ["a.py"],
            "writes": [],
            "description": "review 1",
        },
        {
            "task_id": "I2",
            "parallel_group": 1,
            "assigned_persona": "backend-engineer-1",
            "writes": ["b.py"],
            "depends_on": [],
            "reads": [],
            "description": "impl 2",
        },
        {
            "task_id": "R2",
            "parallel_group": 2,
            "assigned_persona": "code-reviewer-1",
            "reviews": "I2",
            "depends_on": ["I2"],
            "reads": ["b.py"],
            "writes": [],
            "description": "review 2",
        },
    ]
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    seen = {"R1": 0, "R2": 0}

    def policy(rid):
        def f(attempt):
            seen[rid] += 1
            return _env(rid, attempt, status="done" if seen[rid] >= 2 else "blocked")

        return f

    runner = _VerdictRunner(
        verdicts={"R1": policy("R1"), "R2": policy("R2")},
        writes={"I1": ["a.py"], "I2": ["b.py"]},
        # A real suspension delay INSIDE the instrumented critical section so two
        # concurrently-admitted dispatches genuinely OVERLAP on the loop — without it
        # the probe is a tautology (the runner runs to completion synchronously, so
        # max_concurrency could never reach 2 even with a leaked 2nd semaphore).
        delay=0.02,
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"I1": "R1", "R1": "I1", "I2": "R2", "R2": "I2"}
    # Cap the global fleet at 1 so a SECOND semaphore would show as concurrency 2.
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone, runner, journal, budget, dag_proof, review_pairing=pairing, max_workers=1
            ),
        )
    )
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    assert all(by_id[t]["status"] == "done" for t in ("I1", "I2", "R1", "R2")), res
    # SEMAPHORE REUSE: with max_workers=1 the SINGLE shared semaphore caps
    # concurrency at 1. A second semaphore in the sub-procedure would let an inner
    # review dispatch run while an outer impl held the outer slot → concurrency 2.
    assert runner.max_concurrency == 1, (
        f"max concurrency {runner.max_concurrency} > 1 → a SECOND semaphore "
        "(double the global cap) leaked into the review-fix loop"
    )
    # BUBBLING: every dispatch charged output_tokens; the parent's spent() reflects
    # the FULL count (4 impl/review base dispatches + 2 fix dispatches + 2 re-reviews
    # = 8 charges x 5 tokens = 40), i.e. the child charges bubbled up.
    total_dispatches = sum(runner.dispatch_count.values())
    assert budget.spent() == total_dispatches * 5, (
        f"parent spent {budget.spent()} != {total_dispatches * 5} → child charges "
        "did NOT bubble to the parent pool"
    )
    assert budget.usage_breakdown()["output_tokens"] == budget.spent()


# ── Iron-Law 6: reviewer ABANDON cascades, no double self-escalation ──────────


def test_reviewer_abandon_cascades_implement_no_double_self_escalation(tmp_path):
    """The reviewer ABANDONS (worker failure, NOT a BLOCK verdict). Assert:
    * the implement is NOT re-dispatched (a broken reviewer cannot grade);
    * a task DEPENDENT on the reviewer cascades blocked (upstream=reviewer) once;
    * the escalations carry the reviewer self-escalation AND the cascade
      escalation under DIFFERENT categories, with NO duplicate of either.
    """
    clone = _git_init_clone(tmp_path)
    tasks = _impl_review_tasks()
    # A task that depends on the REVIEW output (so a reviewer abandon cascades it).
    tasks.append(
        {
            "task_id": "DOWN",
            "parallel_group": 3,
            "assigned_persona": "technical-writer-1",
            "depends_on": ["REVIEW"],
            "reads": [],
            "writes": ["d.py"],
            "description": "downstream of the review",
        }
    )
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    runner = _VerdictRunner(
        # The reviewer worker AUTHORS a `failed` envelope — a WORKER FAILURE
        # (self-escalates under category "failed"), NOT a `blocked` verdict. Path A
        # parity: a worker `failed` envelope is a cascade source that self-escalates.
        verdicts={"REVIEW": lambda a: _env("REVIEW", a, status="failed", artifacts=False)},
        writes={"IMPL": ["a.py"], "DOWN": ["d.py"]},
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"IMPL": "REVIEW", "REVIEW": "IMPL"}
    escalations: list[dict] = []
    res = _run(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                review_pairing=pairing,
                escalate_fn=escalations.append,
            ),
        )
    )
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    # The full ordered results (incl. non-dict _FailedAttempt sentinels) keyed by
    # the deterministic (parallel_group, task_id) order of the input tasks.
    ordered_ids = [
        t["task_id"] for t in sorted(tasks, key=lambda t: (t["parallel_group"], t["task_id"]))
    ]
    res_by_id = dict(zip(ordered_ids, res, strict=True))
    # IMPL succeeded and was NOT re-dispatched (reviewer abandon ≠ block verdict).
    assert by_id["IMPL"]["status"] == "done"
    assert runner.dispatch_count["IMPL"] == 1, runner.dispatches
    # REVIEW dispatched once (the failed worker) and is a non-success.
    assert runner.dispatch_count["REVIEW"] == 1, runner.dispatches
    rev = res_by_id["REVIEW"]
    assert is_failed_attempt(rev) or (isinstance(rev, dict) and rev.get("status") != "done")
    # DOWN cascaded blocked on the abandoned reviewer (upstream=REVIEW), once.
    assert by_id["DOWN"]["status"] == "abandoned"
    assert by_id["DOWN"]["category"] == "blocked"
    assert by_id["DOWN"]["upstream_task_id"] == "REVIEW"
    # Exactly ONE cascade escalation for DOWN (blocked) — never duplicated.
    cats_by_task: dict[str, list[str]] = {}
    for e in escalations:
        cats_by_task.setdefault(e["task_id"], []).append(e["category"])
    # The REVIEWER self-escalates (worker `failed` → category "failed").
    assert cats_by_task.get("REVIEW") == ["failed"], escalations
    # DOWN cascades blocked exactly once.
    assert cats_by_task.get("DOWN") == ["blocked"], escalations
    # The two escalations are under DIFFERENT categories.
    assert "failed" != "blocked"
    # No task escalated the SAME category twice (no double self-escalation).
    for tid, cats in cats_by_task.items():
        assert len(cats) == len(set(cats)), f"{tid} double-escalated: {cats}"


# ── No-op: empty/None pairing leaves pipeline byte-identical ──────────────────


def test_empty_pairing_is_noop_byte_identical(tmp_path):
    """LITERAL byte-identical no-op: a task list carrying `reviews` but dispatched
    with NO review_pairing produces the EXACT SAME ordered results as the identical
    tasks with the `reviews` field STRIPPED (the pure pre-M6b-1 M5 codepath). The
    full ordered results lists are compared element-for-element — not just dispatch
    counts — so "byte-identical" is literally true.
    """

    def _make():
        # A reviewer that would BLOCK forever IF the loop ran — so any accidental
        # loop activation diverges loudly from the M5 baseline.
        runner = _VerdictRunner(
            verdicts={"REVIEW": lambda a: _env("REVIEW", a, status="blocked")},
            writes={"IMPL": ["a.py"]},
        )
        return runner, ResultJournal(), BudgetPool(total_tokens=10_000_000)

    # Run A — tasks WITH `reviews`, pairing OMITTED (the M6b-1 gated path, inert).
    clone_a = _git_init_clone(tmp_path / "a")
    tasks_a = _impl_review_tasks()
    dag_a = compute_dag_proof([dict(t) for t in tasks_a])
    runner_a, journal_a, budget_a = _make()
    res_a = _run(
        pipeline(tasks_a, **_pipeline_kwargs(clone_a, runner_a, journal_a, budget_a, dag_a))
    )

    # Run B — the SAME tasks with the `reviews` field STRIPPED (pure M5; the engine
    # can never see a pairing). The reviewer becomes a plain read-only task.
    clone_b = _git_init_clone(tmp_path / "b")
    tasks_b = _impl_review_tasks()
    for t in tasks_b:
        t.pop("reviews", None)
    dag_b = compute_dag_proof([dict(t) for t in tasks_b])
    runner_b, journal_b, budget_b = _make()
    res_b = _run(
        pipeline(tasks_b, **_pipeline_kwargs(clone_b, runner_b, journal_b, budget_b, dag_b))
    )

    # BYTE-IDENTICAL: the full ordered results lists are equal element-for-element.
    assert res_a == res_b, (res_a, res_b)
    # And dispatch counts match exactly (each task once — NO loop, NO re-dispatch).
    assert runner_a.dispatch_count == runner_b.dispatch_count == {"IMPL": 1, "REVIEW": 1}
    # Sanity: the reviewer's lone BLOCK is just a terminal non-success (cascade
    # source) — exactly the pre-M6b-1 semantics, identical on both runs.
    by_id = {r["task_id"]: r for r in res_a if isinstance(r, dict)}
    assert by_id["IMPL"]["status"] == "done"
    assert by_id["REVIEW"]["status"] == "blocked"


# ── FIX 5: fix-fails path records the reviewer's REAL BLOCK verdict ───────────


def test_fix_fails_records_reviewer_real_block_verdict_not_cascade(tmp_path):
    """When a reviewer BLOCKs and the subsequent IMPLEMENT fix re-dispatch itself
    FAILS, the reviewer's terminal record must be its OWN worker-authored BLOCK
    verdict (status='blocked', the real envelope) — NOT a structured
    blocked-CASCADE stamped by the admission gate. Higher abandonment-report
    fidelity, and proves no double-finalize (the reviewer is terminal exactly
    once, escalated exactly once).
    """
    clone = _git_init_clone(tmp_path)
    tasks = _impl_review_tasks()
    dag_proof = compute_dag_proof([dict(t) for t in tasks])
    impl_runs = {"n": 0}

    def impl_policy(attempt):
        impl_runs["n"] += 1
        # 1st implement DONE (reviewable); the FIX re-dispatch FAILS (is_error).
        return _env("IMPL", attempt, status="done") if impl_runs["n"] == 1 else "FAILED"

    runner = _VerdictRunner(
        verdicts={
            "IMPL": impl_policy,
            # The reviewer authors a real BLOCK verdict (a deliberate review finding).
            "REVIEW": lambda a: _env("REVIEW", a, status="blocked"),
        },
        writes={"IMPL": ["a.py"]},
    )
    journal = ResultJournal()
    budget = BudgetPool(total_tokens=10_000_000)
    pairing = {"IMPL": "REVIEW", "REVIEW": "IMPL"}
    escalations: list[dict] = []
    res = _run_bounded(
        pipeline(
            tasks,
            **_pipeline_kwargs(
                clone,
                runner,
                journal,
                budget,
                dag_proof,
                review_pairing=pairing,
                escalate_fn=escalations.append,
            ),
        )
    )
    ordered_ids = [
        t["task_id"] for t in sorted(tasks, key=lambda t: (t["parallel_group"], t["task_id"]))
    ]
    res_by_id = dict(zip(ordered_ids, res, strict=True))
    by_id = {r["task_id"]: r for r in res if isinstance(r, dict)}
    # IMPL ran twice (initial DONE + one failing fix) then is the failure.
    assert runner.dispatch_count["IMPL"] == 2, runner.dispatches
    # The reviewer's terminal record is its OWN BLOCK verdict — NOT a structured
    # cascade (which would carry category='blocked' + upstream_task_id + the engine
    # abandon marker). A real worker BLOCK envelope has status='blocked' and NO
    # 'upstream_task_id'/'category' engine-abandon keys.
    rev = by_id["REVIEW"]
    assert rev["status"] == "blocked", rev
    assert "upstream_task_id" not in rev, f"reviewer recorded as a CASCADE, not its verdict: {rev}"
    assert rev.get("type") == "task_result", (
        f"reviewer record must be its worker envelope, not a structured abandon: {rev}"
    )
    # No double-finalize / double-escalate: the reviewer escalates at most once.
    rev_escs = [e for e in escalations if e.get("task_id") == "REVIEW"]
    assert len(rev_escs) <= 1, rev_escs
    # IMPL is a failed terminal (the fix failed) — the failure sentinel or a non-done.
    impl_res = res_by_id["IMPL"]
    assert is_failed_attempt(impl_res) or (
        isinstance(impl_res, dict) and impl_res.get("status") != "done"
    )
