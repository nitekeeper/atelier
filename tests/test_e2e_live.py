"""END-TO-END LIVE validation of the deterministic-host engine (M0-M4).

This is the de-risking validation the unit/integration suite cannot give: the
whole REAL stack driven on a throwaway git repo —

    task DAG  →  host_scheduler.pipeline()  →  CliDispatchTools leaf
              →  real_cli_runner (a real `claude -p` subprocess)
              →  native_sandbox_wrap (Claude Code's native OS-level confinement)
              →  BudgetPool metering  →  ResultJournal content-addressed cache

Everything else in the test suite uses M3's ``FakeCliRunner`` (no real process);
this file proves the engine works against a REAL ``claude``.

Marked ``@pytest.mark.live`` (deselected by the default gate). It SKIPS cleanly
unless BOTH ``claude`` is on PATH AND the platform's native sandbox runtime is
available:

* ``claude`` — the real CLI the engine spawns (subscription auth; no
  ``ANTHROPIC_API_KEY`` needed);
* the **native sandbox runtime** (``sandbox_runtime_available()``), required
  because this harness NEVER opts out of the mandatory-sandbox gate.
  ``run_attempt`` refuses to spawn a real, write-capable agent without a real OS
  sandbox (the permission layer does NOT confine writes — proven live), and
  ``ATELIER_CLI_ALLOW_UNSANDBOXED`` is deliberately NEVER set here.
  ``native_sandbox_wrap`` is fail-closed: when the platform sandbox can't
  initialize the ``claude`` CLI refuses to start. The runtime is **cross-platform**:
  on **Linux/WSL2** it needs ``bwrap`` + ``socat``; on **macOS** the Seatbelt
  sandbox is built-in (zero installs → the test RUNS on a Mac with no installs).
  So we require the sandbox runtime to be present before this test can do real
  work — otherwise it would only ever exercise the refuse-to-start path, not the
  engine.

On a host missing the sandbox runtime (e.g. Linux without ``bwrap``/``socat``)
the test SKIPS with a platform-correct reason; it runs for real once the runtime
is available (or immediately on macOS).

The DAG (the M0 §1.1 shape):

    T1 writes a.txt   ┐  (write-disjoint, proven independent → pipeline-able,
    T2 writes b.txt   ┘   barrier-free concurrent advance)
    T3 reads a.txt,b.txt; writes c.txt; depends_on [T1, T2]   (a REAL barrier)

The validation contract (the 5 assertions) is documented inline at the asserts.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    DEFAULT_DISALLOWED_TOOLS,
    is_failed_attempt,
    native_sandbox_wrap,
    real_cli_runner,
    sandbox_prereq_status,
    sandbox_runtime_available,
)
from scripts.dag import compute_dag_proof
from scripts.host_scheduler import pipeline, simple_worktree_factory
from scripts.result_journal import ResultJournal

pytestmark = pytest.mark.live

# ── prerequisite probes (skip cleanly, do not error) ─────────────────────────
# BOTH are required: `claude` is the real CLI the engine spawns; the platform's
# native sandbox runtime is the OS sandbox the mandatory-sandbox gate demands (we
# NEVER opt out via ATELIER_CLI_ALLOW_UNSANDBOXED). Without it, native_sandbox_wrap
# is fail-closed (claude refuses to start) and no real engine work would happen.
# `sandbox_runtime_available()` is cross-platform: Linux/WSL2 → needs bwrap+socat;
# macOS → built-in Seatbelt (always available, zero installs).
_HAS_CLAUDE = shutil.which("claude") is not None
_HAS_SANDBOX = sandbox_runtime_available()

if not _HAS_CLAUDE:
    _SKIP_REASON = "prerequisite missing: `claude` not on PATH (the engine spawns a real claude -p)"
elif not _HAS_SANDBOX:
    _SKIP_REASON = (
        "prerequisite missing: native sandbox runtime unavailable — this harness "
        "runs SANDBOXED only (never sets ATELIER_CLI_ALLOW_UNSANDBOXED) and "
        "native_sandbox_wrap is fail-closed, so the engine cannot do real work. "
        + sandbox_prereq_status()[1]
    )
else:
    _SKIP_REASON = ""

# Generous per-attempt wall clock: a real `claude` call takes seconds. This is
# bounded by the engine's WALL_CLOCK_S (run_attempt asserts adapter <= engine),
# and the default pipeline wall_clock_s already equals WALL_CLOCK_S, so we rely
# on that default rather than overriding it.

# A generous budget ceiling so the budget gate never trips during the run — we
# WANT all three tasks to spawn (this validates real metering, not the gate).
_GENEROUS_BUDGET_TOKENS = 10_000_000


def _git(args: list[str], cwd) -> None:
    """Run a git command in *cwd*, raising on failure. Identity is supplied
    per-invocation via ``-c user.*`` (the engine convention) so a clean/CI host
    with no global git identity still commits successfully."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_throwaway_clone(tmp_path):
    """Build a real throwaway git clone with an initial commit.

    Explicit ``-c user.name``/``-c user.email`` per the engine convention — CI /
    clean envs have no global git identity, and the engine itself commits writer
    worktrees with its own fixed ``atelier``/``atelier@localhost`` identity.
    """
    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], clone)
    (clone / "seed.txt").write_text("seed\n")
    _git(["add", "-A"], clone)
    _git(
        ["-c", "user.name=atelier", "-c", "user.email=atelier@localhost", "commit", "-qm", "init"],
        clone,
    )
    return clone


def _tasks():
    """The T1/T2 (barrier-free) + T3 (barrier) DAG.

    Task-dict shape is exactly what host_scheduler + compute_dag_proof consume:
    ``task_id`` / ``parallel_group`` / ``depends_on`` / ``reads`` / ``writes`` /
    ``assigned_persona`` / ``phase``. T1,T2 are write-disjoint and in the same
    wave → proven independent → barrier-free. T3 is a strictly-later wave that
    reads both upstream writes and depends_on both → a REAL barrier edge.
    """
    return [
        {
            "task_id": "T1",
            "parallel_group": 0,
            "writes": ["a.txt"],
            "assigned_persona": "backend-engineer-1",
            "phase": "build",
        },
        {
            "task_id": "T2",
            "parallel_group": 0,
            "writes": ["b.txt"],
            "assigned_persona": "backend-engineer-1",
            "phase": "build",
        },
        {
            "task_id": "T3",
            "parallel_group": 1,
            "reads": ["a.txt", "b.txt"],
            "writes": ["c.txt"],
            "depends_on": ["T1", "T2"],
            "assigned_persona": "backend-engineer-1",
            "phase": "build",
        },
    ]


# Trivial deterministic content each task must write. T3's content is derived
# from a.txt + b.txt so the barrier (T3 reads T1's + T2's outputs) is meaningful.
_EXPECTED = {"a.txt": "A", "b.txt": "B", "c.txt": "AB"}


def _briefing_for(task, attempt):
    """Build the per-task system-prompt briefing.

    The briefing instructs the agent (via the Write/Read tools — Bash/WebFetch/
    WebSearch are denied) to create exactly the right file with trivial
    deterministic content in the CURRENT directory (the engine pins cwd to the
    task's worktree / clone), then emit the terminal envelope whose ``task_id``
    and ``attempt`` MATCH the host's dispatch identity (validate_envelope is
    anti-spoof: it re-checks the model's self-reported id/attempt against the
    dispatch row). Untrusted-input note: the task dict is host-authored here, so
    this is a trusted briefing — but we keep it minimal and explicit.
    """
    tid = str(task["task_id"])
    writes = list(task.get("writes") or [])
    reads = list(task.get("reads") or [])
    out_file = writes[0]
    content = _EXPECTED[out_file]

    lines = [
        "You are a backend engineer running one deterministic file-write task in "
        "an isolated git working directory. Do EXACTLY what is asked, nothing more.",
        "Your current working directory is ALREADY the correct target directory. "
        "All file paths below are RELATIVE to it — use the bare relative filename "
        "exactly as written. Do NOT prepend any absolute path, do NOT try `/tmp`, "
        "`/workspace`, your home directory, or any other location, and do NOT "
        "change directories.",
    ]
    if reads:
        lines.append(
            "First, using the Read tool, read these files (relative to the current "
            "directory) to confirm they exist: " + ", ".join(f"`{r}`" for r in reads) + "."
        )
    lines.append(
        f"Using the Write tool, create the file at the relative path `{out_file}` "
        f"(in the current working directory) containing EXACTLY the text `{content}` "
        "(no trailing newline, no extra characters, no surrounding quotes)."
    )
    lines.append(
        "If — and ONLY if — the Write succeeds, THEN emit ONLY the terminal "
        "task_result envelope matching the provided json-schema, with status exactly "
        f"'done', task_id exactly '{tid}', attempt exactly {attempt}, exactly one "
        f'artifact {{"path": "{out_file}", "sha": "deadbeef"}}, and a one-line '
        "notes_md. If the Write is denied or otherwise fails, do NOT report 'done' — "
        "emit the envelope with status 'blocked' and a notes_md naming the error. "
        "Do nothing else."
    )
    return "\n".join(lines)


def _model_for(task, attempt):
    """Cheapest tier for every task — the CLI resolves the `haiku` alias to the
    current Haiku model (matches the live-smoke pattern)."""
    return "haiku"


def _summary_line(task_id, env, *, replayed=False):
    if is_failed_attempt(env):
        return f"  {task_id}: FAILED_ATTEMPT ({getattr(env, 'reason', '')!r})"
    tag = " [journal-replay, $0]" if replayed else ""
    return f"  {task_id}: status={env.get('status')!r}{tag}"


@pytest.mark.skipif(not (_HAS_CLAUDE and _HAS_SANDBOX), reason=_SKIP_REASON)
def test_e2e_live_full_stack_dag_through_pipeline(tmp_path, capsys):
    """Drive the WHOLE real engine through ``host_scheduler.pipeline()`` on a
    throwaway repo: a 3-task DAG (T1/T2 barrier-free, T3 barrier) executed by a
    real sandboxed ``claude``, metered, journaled, then replayed at zero cost.

    The 5-assertion validation contract is checked inline below.
    """
    clone = _init_throwaway_clone(tmp_path)
    tasks = _tasks()

    # The DAG proof: T1,T2 proven independent (write-disjoint, same wave, no dep);
    # T3 has a real reads-from + depends_on barrier on both. `seed.txt` pre-exists
    # so the reads-satisfiable gate is keyed on T1/T2's writes, not the seed.
    existing = {"seed.txt"}
    dag_proof = compute_dag_proof(tasks, existing_files=existing)
    # Sanity (un-asserted-as-contract, but documents the DAG's shape):
    assert dag_proof.independent("T1", "T2")  # barrier-free pair
    assert not dag_proof.independent("T1", "T3")  # T3 barrier on T1
    assert not dag_proof.independent("T2", "T3")  # T3 barrier on T2
    assert dag_proof.reads_from("T3") == frozenset({"T1", "T2"})  # the real barrier

    # A persisted journal under tmp_path (so the SECOND run replays from disk).
    journal_path = tmp_path / "journal.json"
    budget = BudgetPool(total_tokens=_GENEROUS_BUDGET_TOKENS)

    # ── RUN 1: real engine, real sandboxed claude ───────────────────────────
    # pipeline() IS the top-level barrier-free entrypoint (tests/test_host_scheduler
    # construct it exactly this way). It builds the JournalKeyTracker + threads
    # upstream hashes internally; we supply the real runner + the native sandbox +
    # a worktree_factory so concurrent writers (T1/T2) are physically isolated and
    # merged back deterministically into the base clone.
    journal1 = ResultJournal(journal_path)
    results = asyncio.run(
        pipeline(
            tasks,
            budget=budget,
            journal=journal1,
            dag_proof=dag_proof,
            model_for=_model_for,
            briefing_for=_briefing_for,
            clone_dir=str(clone),
            worktree_factory=simple_worktree_factory(clone),
            runner=real_cli_runner,
            disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
            sandbox_wrap=native_sandbox_wrap(str(clone)),  # SANDBOXED — never unsandboxed
        )
    )

    # pipeline() returns results in deterministic (parallel_group, task_id) order:
    # [T1, T2, T3].
    by_id = {
        t["task_id"]: r
        for t, r in zip(
            sorted(tasks, key=lambda t: (t["parallel_group"], t["task_id"])), results, strict=True
        )
    }

    # ── ASSERTION 1: all 3 tasks returned a terminal, schema-valid `done`
    #    envelope from a REAL claude (validate_envelope already ran inside
    #    run_attempt against the host's dispatch identity — a failed attempt or
    #    spoofed id would be FAILED_ATTEMPT, not a `done` envelope). ───────────
    for tid in ("T1", "T2", "T3"):
        env = by_id[tid]
        assert not is_failed_attempt(env), f"{tid}: live attempt FAILED: {env!r}"
        assert env["type"] == "task_result", f"{tid}: not a task_result envelope: {env!r}"
        assert str(env["task_id"]) == tid, f"{tid}: envelope task_id mismatch: {env!r}"
        assert env["status"] == "done", f"{tid}: not done: {env!r}"

    # ── ASSERTION 2: a.txt/b.txt/c.txt actually exist in the BASE clone after
    #    the worktree merge-back, with the expected trivial content. ──────────
    for fname, content in _EXPECTED.items():
        path = clone / fname
        assert path.exists(), f"{fname} missing from base clone after merge"
        assert path.read_text().strip() == content, (
            f"{fname} content {path.read_text()!r} != expected {content!r}"
        )

    # ── ASSERTION 3: real metering — budget.spent() > 0 and usage_breakdown()
    #    shows real input AND output tokens (the in-band meter the bridge never
    #    had). ─────────────────────────────────────────────────────────────────
    assert budget.spent() > 0, "no output tokens metered — real claude did not run?"
    breakdown = budget.usage_breakdown()
    assert breakdown["output_tokens"] > 0, f"no output tokens in breakdown: {breakdown}"
    assert breakdown["input_tokens"] > 0, f"no input tokens in breakdown: {breakdown}"
    spent_after_run1 = budget.spent()

    # ── ASSERTION 4: the ResultJournal has 3 entries (one per task). Re-open the
    #    persisted file to prove it survived to disk. ─────────────────────────
    journal_reopened = ResultJournal(journal_path)
    n_entries = len(journal_reopened._store)
    assert n_entries == 3, f"journal has {n_entries} entries, expected 3"

    # ── ASSERTION 5: a SECOND run of the SAME DAG makes ZERO new claude spawns
    #    (journal replay at $0). We wrap real_cli_runner in a spawn counter; the
    #    journal hit short-circuits BEFORE the runner is ever called, so the
    #    counter MUST be 0 and budget spend MUST NOT increase. ─────────────────
    spawn_count = {"n": 0}

    async def counting_runner(argv, cwd):
        spawn_count["n"] += 1
        return await real_cli_runner(argv, cwd)

    # Preserve the fail-closed realness marker so the mandatory-sandbox gate still
    # treats this as a real runner (it would anyway — unmarked == real — but be
    # explicit; the sandbox is wired, so the gate passes).
    counting_runner.spawns_real_process = True  # type: ignore[attr-defined]

    journal2 = ResultJournal(journal_path)  # same persisted file → all hits
    results2 = asyncio.run(
        pipeline(
            tasks,
            budget=budget,
            journal=journal2,
            dag_proof=dag_proof,
            model_for=_model_for,
            briefing_for=_briefing_for,
            clone_dir=str(clone),
            worktree_factory=simple_worktree_factory(clone),
            runner=counting_runner,
            disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
            sandbox_wrap=native_sandbox_wrap(str(clone)),
        )
    )
    by_id2 = {
        t["task_id"]: r
        for t, r in zip(
            sorted(tasks, key=lambda t: (t["parallel_group"], t["task_id"])), results2, strict=True
        )
    }
    assert spawn_count["n"] == 0, (
        f"journal replay FAILED: {spawn_count['n']} new claude spawns on rerun "
        "(expected 0 — every task should be a journal hit)"
    )
    assert budget.spent() == spent_after_run1, "journal replay charged budget (expected $0)"
    for tid in ("T1", "T2", "T3"):
        assert not is_failed_attempt(by_id2[tid]), f"{tid}: replay returned a failed attempt"
        assert by_id2[tid]["status"] == "done", f"{tid}: replay not done"

    # ── Human-readable summary (visible under `-s`) ─────────────────────────
    cost_est_usd = budget.spent() / 1_000_000 * 4.0  # ~$4/Mtok Haiku output (rough)
    lines = [
        "",
        "=== E2E LIVE deterministic-host engine validation ===",
        "RUN 1 (real sandboxed claude):",
        *[_summary_line(tid, by_id[tid]) for tid in ("T1", "T2", "T3")],
        f"  metered: output={breakdown['output_tokens']} input={breakdown['input_tokens']} "
        f"cache_read={breakdown['cache_read_input_tokens']} "
        f"cache_create={breakdown['cache_creation_input_tokens']} tokens",
        f"  rough output-cost estimate: ~${cost_est_usd:.4f}",
        f"  base clone files: a.txt={_EXPECTED['a.txt']!r} b.txt={_EXPECTED['b.txt']!r} "
        f"c.txt={_EXPECTED['c.txt']!r}",
        f"  journal entries: {n_entries}",
        "RUN 2 (journal replay):",
        *[_summary_line(tid, by_id2[tid], replayed=True) for tid in ("T1", "T2", "T3")],
        f"  new claude spawns on rerun: {spawn_count['n']} (expected 0)",
        f"  budget spend unchanged: {budget.spent() == spent_after_run1}",
        "=== PASS ===",
        "",
    ]
    with capsys.disabled():
        print("\n".join(lines))
