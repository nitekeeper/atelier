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
