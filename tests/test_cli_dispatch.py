"""Tests for scripts/cli_dispatch.py — the real ``claude -p`` adapter (M3).

ALL tests run against :class:`scripts.cli_dispatch.FakeCliRunner` — NO real
``claude`` is ever invoked. The fake records every ``(argv, cwd)`` and returns a
configurable canned result mirroring the verified ``claude --output-format json``
shape.

Coverage (per M3 acceptance):
  (a) the EXACT argv is asserted — ``--json-schema`` is ENVELOPE_SCHEMA,
      ``--model`` is the passed tier, ``--system-prompt`` is the briefing,
      ``--permission-mode bypassPermissions``, ``--add-dir`` / cwd is the clone;
  (b) the envelope is validated via the REAL ``validate_envelope``;
  (c) ``budget.charge`` reflects ``usage.output_tokens``;
  (d) a journal HIT returns WITHOUT calling the runner (runner call-count 0 on
      the 2nd identical dispatch);
  (e) ``is_error`` / missing ``structured_output`` / malformed → failed attempt,
      NOT journaled;
  (f) ``BudgetExceeded`` is raised BEFORE the runner is called.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scripts.budget_pool import BudgetExceeded, BudgetPool
from scripts.cli_dispatch import (
    ENVELOPE_SCHEMA,
    FakeCliRunner,
    is_failed_attempt,
    run_attempt,
)
from scripts.result_journal import ResultJournal


def _task(task_id="t-1", **extra):
    base = {"task_id": task_id, "assigned_persona": "backend-engineer-1", "phase": "tdd:green"}
    base.update(extra)
    return base


def _envelope(task_id="t-1", attempt=1, status="done"):
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "done",
        "next_action": "review",
    }


def _run(coro):
    return asyncio.run(coro)


# ── (a) exact argv ──────────────────────────────────────────────────────────


def test_argv_is_exact_list_no_shell(tmp_path):
    """The dispatched argv is a LIST with the verified flags: --json-schema is
    ENVELOPE_SCHEMA, --model is the tier, --system-prompt is the briefing,
    --permission-mode bypassPermissions, --add-dir + cwd are the clone."""
    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True)
    runner = FakeCliRunner(structured_output=_envelope())
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    env = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="opus",
            briefing="SYSTEM BRIEFING BODY",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["status"] == "done"

    assert runner.call_count == 1
    call = runner.calls[0]
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "claude"
    assert argv[1] == "-p"

    # Flag/value pairs are present and correct.
    def val(flag):
        return argv[argv.index(flag) + 1]

    assert val("--output-format") == "json"
    assert json.loads(val("--json-schema")) == ENVELOPE_SCHEMA
    assert val("--model") == "opus"
    assert val("--system-prompt") == "SYSTEM BRIEFING BODY"
    # SECURITY POSTURE (M3 review hardening): default is acceptEdits, NOT
    # bypassPermissions (which is refused unsandboxed); Bash/WebFetch/WebSearch
    # are denied by default.
    assert val("--permission-mode") == "acceptEdits"
    assert val("--add-dir") == str(clone)
    # The deny-list flag is present and carries exactly the three default tools.
    assert "--disallowedTools" in argv
    di = argv.index("--disallowedTools")
    assert argv[di + 1 : di + 4] == ["Bash", "WebFetch", "WebSearch"]
    # No --allowedTools by default (allowlist is opt-in).
    assert "--allowedTools" not in argv
    # cwd is the clone.
    assert call["cwd"] == str(clone)


# ── (b) real validate_envelope ──────────────────────────────────────────────


def test_envelope_validated_via_real_validator_anti_spoof(tmp_path):
    """A structured_output whose task_id MISMATCHES the dispatched task is
    rejected by the REAL validate_envelope → failed attempt (not journaled)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    # Worker claims a DIFFERENT task_id than the one dispatched (spoof).
    spoofed = _envelope(task_id="OTHER-TASK")
    runner = FakeCliRunner(structured_output=spoofed)
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    result = _run(
        run_attempt(
            _task(task_id="t-1"),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert is_failed_attempt(result)
    # Not journaled, and the budget was NOT charged for a rejected envelope.
    assert budget.spent() == 0


def test_well_formed_envelope_accepted(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope(task_id="t-9", attempt=2))
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    env = _run(
        run_attempt(
            _task(task_id="t-9"),
            2,
            budget=budget,
            journal=journal,
            model="haiku",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["type"] == "task_result"
    assert env["status"] == "done"


# ── (c) budget.charge reflects usage.output_tokens ──────────────────────────


def test_budget_charge_reflects_output_tokens(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(
        structured_output=_envelope(),
        usage={"output_tokens": 123, "input_tokens": 45, "cache_read_input_tokens": 6},
    )
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert budget.spent() == 123
    # Side counters bubble for reconciliation but never gate.
    assert budget.usage_breakdown()["input_tokens"] == 45
    assert budget.usage_breakdown()["cache_read_input_tokens"] == 6


# ── (d) journal HIT returns without calling the runner ──────────────────────


def test_journal_hit_skips_runner(tmp_path):
    """The 2nd identical dispatch is a journal HIT: it returns the cached
    envelope WITHOUT calling the runner (runner call-count stays 1)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    kwargs = {
        "budget": budget,
        "journal": journal,
        "model": "sonnet",
        "briefing": "identical-briefing",
        "clone_dir": str(clone),
        "runner": runner,
    }
    first = _run(run_attempt(_task(), 1, **kwargs))
    assert first["status"] == "done"
    assert runner.call_count == 1
    spent_after_first = budget.spent()

    # Second identical dispatch → journal hit, NO runner call, NO extra charge.
    second = _run(run_attempt(_task(), 1, **kwargs))
    assert second == first
    assert runner.call_count == 1  # still 1 — the runner was NOT invoked again
    assert budget.spent() == spent_after_first  # no double charge on replay


def test_journal_miss_on_changed_upstream(tmp_path):
    """A changed upstream-envelope-hash set changes the key → cache miss →
    the runner IS called again."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    kwargs = {
        "budget": budget,
        "journal": journal,
        "model": "sonnet",
        "briefing": "b",
        "clone_dir": str(clone),
        "runner": runner,
    }
    _run(run_attempt(_task(), 1, upstream_envelope_hashes=["h1"], **kwargs))
    assert runner.call_count == 1
    _run(run_attempt(_task(), 1, upstream_envelope_hashes=["h2"], **kwargs))
    assert runner.call_count == 2  # different upstream digest → miss → re-run


# ── (e) is_error / missing structured_output / malformed → failed, not journaled ─


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_error": True},
        {"subtype": "error_max_structured_output_retries"},
        {"structured_output": None},  # missing structured_output
    ],
)
def test_error_results_are_failed_attempts_not_journaled(tmp_path, kwargs):
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(**kwargs)
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    result = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert is_failed_attempt(result)
    assert budget.spent() == 0  # a failed attempt charges nothing
    # NOT journaled: a fresh identical dispatch would call the runner again.
    runner2 = FakeCliRunner(structured_output=_envelope())
    _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner2,
        )
    )
    assert runner2.call_count == 1  # would be 0 if the failed attempt had been journaled


def test_runner_crash_is_failed_attempt(tmp_path):
    """A non-zero exit / crash (runner raises) → failed attempt, not journaled."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(raise_exc=RuntimeError("claude exited 1: boom"))
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    result = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert is_failed_attempt(result)


def test_wall_clock_timeout_is_failed_attempt(tmp_path):
    """A runner that sleeps past the (tiny, injected) wall clock → failed
    attempt via asyncio.wait_for timeout."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope(), sleep=0.2)
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    result = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
            wall_clock_s=0.01,
        )
    )
    assert is_failed_attempt(result)
    assert budget.spent() == 0


# ── (f) BudgetExceeded raised BEFORE the runner is called ───────────────────


def test_budget_exceeded_raised_before_runner(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    # effective_ceiling = 100 * 0.70 = 70. est_for(opus) default = 12_000 >> 70.
    budget = BudgetPool(total_tokens=100)
    journal = ResultJournal()
    with pytest.raises(BudgetExceeded):
        _run(
            run_attempt(
                _task(),
                1,
                budget=budget,
                journal=journal,
                model="opus",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
            )
        )
    # The guard fired PRE-spawn: the runner was never invoked.
    assert runner.call_count == 0


def test_failed_status_envelope_is_journaled_as_terminal(tmp_path):
    """A worker that returns a VALID `failed` envelope (terminal hard-failure)
    is a successful PARSE — validate_envelope accepts it, it IS journaled, and
    the budget is charged. (Distinct from a CLI is_error, which is a failed
    ATTEMPT.) The engine's terminal routing of `failed` is the WaveDispatcher's
    job; here we assert the adapter returns the validated envelope."""
    clone = tmp_path / "clone"
    clone.mkdir()
    failed_env = {
        "type": "task_result",
        "task_id": "t-1",
        "attempt": 1,
        "status": "failed",
        "artifacts": [],
        "notes_md": "unrecoverable",
        "next_action": "escalate",
    }
    runner = FakeCliRunner(structured_output=failed_env)
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert not is_failed_attempt(env)
    assert env["status"] == "failed"
    assert budget.spent() > 0  # a parsed terminal envelope IS charged + journaled


# ── M5 (6): transient-spawn retry INSIDE the one charged attempt ────────────


class _FlakySpawnRunner(FakeCliRunner):
    """A FakeCliRunner that raises a TRANSIENT OSError on the first
    ``fail_first`` calls (simulating EAGAIN / transient fork failure on spawn),
    then returns a valid envelope.  Records every call so the test can assert
    the retry happened INSIDE one engine attempt."""

    def __init__(self, *, fail_first: int = 1, **kw):
        super().__init__(**kw)
        self._fail_first = fail_first
        self._spawn_calls = 0

    async def __call__(self, argv, cwd):
        self._spawn_calls += 1
        if self._spawn_calls <= self._fail_first:
            # Record the attempted call (so call_count reflects spawn attempts),
            # then raise a transient OS-level spawn error.
            self.calls.append({"argv": list(argv), "cwd": cwd})
            raise OSError(11, "Resource temporarily unavailable")  # EAGAIN
        return await super().__call__(argv, cwd)


def test_run_attempt_transient_spawn_retried_inside_one_attempt(tmp_path, monkeypatch):
    """A clearly-transient OSError on subprocess launch is retried INSIDE the one
    charged engine attempt: run_attempt returns SUCCESS (not _FailedAttempt), the
    runner is called >1 time, the engine attempt number is NOT incremented, and
    budget.charge fires exactly once (on the eventual success)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    # No backoff sleep in tests (keep it fast + deterministic).
    monkeypatch.setattr("scripts.cli_dispatch._TRANSIENT_SPAWN_BACKOFF_S", 0.0, raising=False)
    runner = _FlakySpawnRunner(fail_first=1, structured_output=_envelope())
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    charge_calls = {"n": 0}
    real_charge = budget.charge

    def counting_charge(usage):
        charge_calls["n"] += 1
        return real_charge(usage)

    budget.charge = counting_charge  # type: ignore[method-assign]

    env = _run(
        run_attempt(
            _task(),
            1,  # engine attempt number — must stay 1 through the in-attempt retry
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert not is_failed_attempt(env)
    assert env["status"] == "done"
    # The retry happened INSIDE the one engine attempt: >1 runner call.
    assert runner.call_count > 1, "expected the transient spawn to be retried in-attempt"
    # The returned envelope still carries engine attempt 1 (no increment).
    assert env["attempt"] == 1
    # Budget charged EXACTLY once — on the eventual success, not per retry.
    assert charge_calls["n"] == 1
    assert budget.spent() == 7  # FakeCliRunner default usage output_tokens (charged once)


def test_run_attempt_nonzero_exit_is_terminal_not_retried(tmp_path):
    """A non-zero ``claude`` exit (RuntimeError) is TERMINAL — the model ran and
    failed, so it charges the attempt and is NOT retried as a transient spawn."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(raise_exc=RuntimeError("claude exited 1: boom"))
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()
    result = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert is_failed_attempt(result)
    # RuntimeError is terminal → exactly ONE runner call (no transient retry).
    assert runner.call_count == 1


def test_run_attempt_wall_clock_total_bound_not_multiplied_by_retries(tmp_path, monkeypatch):
    """With the in-attempt retry loop present, the ENTIRE loop stays inside ONE
    asyncio.wait_for(wall_clock_s) TOTAL bound — it is NOT applied fresh per try.

    A runner that hangs (sleeps past the deadline) on EVERY call must be killed
    within wall_clock_s TOTAL and yield a wall-clock-timeout _FailedAttempt — even
    though the retry loop would, if the bound were per-try, multiply the deadline.

    Catches the per-try-multiplication mutation = OUTER wait_for wrapper removed +
    each retry given its own wait_for(wall_clock_s): there, the OSError-caught inner
    TimeoutError (TimeoutError ⊂ OSError) retries the launch 3x → call_count == 3.
    Verified RED against that mutation in a /tmp copy (machine-independent, unlike a
    wall-clock margin). With the correct single outer bound, call_count == 1."""
    import time as _time

    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr("scripts.cli_dispatch._TRANSIENT_SPAWN_BACKOFF_S", 0.0, raising=False)
    # Each call sleeps 0.2s; with wall_clock_s=0.05 a SINGLE wait_for must trip.
    runner = FakeCliRunner(structured_output=_envelope(), sleep=0.2)
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    t0 = _time.monotonic()
    result = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
            wall_clock_s=0.05,
        )
    )
    elapsed = _time.monotonic() - t0
    assert is_failed_attempt(result)
    assert "wall-clock timeout" in result.reason
    assert budget.spent() == 0
    # DETERMINISTIC discriminator (FIX 5 / Obi-V3): the runner is launched exactly
    # ONCE under the single outer wait_for. NOTE asyncio.TimeoutError ⊂ OSError, so
    # a per-try-timeout mutation would let `except OSError` retry the launch 3x →
    # call_count == 3. Machine-independent, unlike a wall-clock margin.
    assert runner.call_count == 1, (
        f"expected ONE launch under the single wall-clock bound, got "
        f"{runner.call_count} — a per-try timeout would retry and multiply the bound"
    )
    # Loose SECONDARY timing guard (demoted): bounded well under N*sleep, generous
    # enough to tolerate CI scheduling jitter on the single-bound path.
    assert elapsed < 0.05 * 4, f"retry loop multiplied the wall-clock bound: {elapsed:.3f}s"


# ── M5 (7): --max-budget-usd second kill lever in the argv ──────────────────


def test_run_attempt_max_budget_usd_in_argv(tmp_path):
    """run_attempt builds ``--max-budget-usd <amount>`` into the argv, derived
    from the per-task BudgetPool output-token estimate with the same headroom
    logic the budget gate applies.  Un-fakeable: inspect the EXACT argv list the
    production code built."""
    from scripts.cli_dispatch import max_budget_usd_for

    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    budget = BudgetPool(total_tokens=100_000)
    journal = ResultJournal()

    env = _run(
        run_attempt(
            _task(),
            1,
            budget=budget,
            journal=journal,
            model="opus",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["status"] == "done"
    argv = runner.calls[0]["argv"]
    assert "--max-budget-usd" in argv, f"--max-budget-usd missing from argv: {argv}"
    bi = argv.index("--max-budget-usd")
    got = argv[bi + 1]
    # FIX 3 (Obi-V1) — PIN the exact value from FIRST-PRINCIPLES constants, NOT by
    # re-calling max_budget_usd_for (that round-trip stays green if the derivation
    # is halved). opus est = 12_000 output tokens:
    #   (12000*25/1e6  output  +  12000*5.0*5/1e6  input  ) * (1/0.70 headroom)
    #   = (0.30 + 0.30) * 1.428571…  = 0.857142…  → "0.86"
    assert got == "0.86", f"argv value {got!r} != first-principles 0.86 for opus est 12000"
    # Cross-check the production fn produces the SAME float we pinned (so the pin
    # and the constants can't silently drift apart without one of them going RED).
    assert f"{max_budget_usd_for(12_000):.2f}" == "0.86"


def test_max_budget_usd_for_derivation_scales_with_estimate():
    """FIX 3 (Obi-V1) — concrete pinned points (not a loose floor): the derivation
    is monotone and matches first-principles arithmetic at known token estimates.

    Per-token rates: output $25/1M, input $5/1M; input allowance = 5x the output
    estimate; headroom = 1/0.70. So for est E tokens:
        usd(E) = (E*25/1e6 + E*5*5/1e6) * (1/0.70) = E * 50/1e6 / 0.70
    haiku est 2_000  → 0.1428571…   opus est 12_000 → 0.8571428…"""
    import pytest as _pytest

    from scripts.cli_dispatch import max_budget_usd_for

    small = max_budget_usd_for(2_000)  # haiku-ish
    big = max_budget_usd_for(12_000)  # opus-ish
    assert big > small > 0.0  # monotone
    # Concrete pins (catch a halved/doubled derivation that a loose floor misses).
    assert small == _pytest.approx(0.14285714285714288, rel=0, abs=1e-9)
    assert big == _pytest.approx(0.8571428571428571, rel=0, abs=1e-9)
    # First-principles closed form for an arbitrary third point (sonnet est 6_000).
    assert max_budget_usd_for(6_000) == _pytest.approx(6_000 * 50 / 1e6 / 0.70, abs=1e-9)
