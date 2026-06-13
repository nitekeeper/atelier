"""M3 review-hardening tests — write-containment posture + lifecycle hygiene.

These pin the BLOCKER fix (the permission/tools/env/sandbox posture) and the
systems findings (zombie reap on timeout, owned-loop FD hygiene, stale-future
guard, wall-clock invariant). All run with NO real ``claude`` — the live
confinement proof lives in ``tests/test_cli_live_smoke.py`` (``-m live``).
"""

from __future__ import annotations

import asyncio
import gc
import warnings

import pytest

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    DEFAULT_DISALLOWED_TOOLS,
    DEFAULT_PERMISSION_MODE,
    UNSANDBOXED_OPT_OUT_ENV,
    CliDispatchTools,
    FakeCliRunner,
    UnsandboxedBypassError,
    UnsandboxedRealRunError,
    _runner_spawns_real_process,
    build_subprocess_env,
    bwrap_sandbox_wrap,
    is_failed_attempt,
    real_cli_runner,
    run_attempt,
)
from scripts.result_journal import ResultJournal


def _task(task_id="t-1"):
    return {"task_id": task_id, "assigned_persona": "be-1", "phase": "tdd:green"}


def _envelope(task_id="t-1", attempt=1, status="done"):
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "done",
    }


def _run(coro):
    return asyncio.run(coro)


# ── BLOCKER: default permission posture is acceptEdits + deny-list ──────────


def test_default_permission_mode_is_accept_edits_not_bypass():
    assert DEFAULT_PERMISSION_MODE == "acceptEdits"
    assert DEFAULT_PERMISSION_MODE != "bypassPermissions"


def test_default_disallowed_tools_block_bash_and_egress():
    assert set(DEFAULT_DISALLOWED_TOOLS) == {"Bash", "WebFetch", "WebSearch"}


def test_bypass_permissions_refused_without_sandbox(tmp_path):
    """`bypassPermissions` is REFUSED when no sandbox is wired (identity wrap)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    with pytest.raises(UnsandboxedBypassError):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
                permission_mode="bypassPermissions",
            )
        )
    # Refused PRE-spawn: the runner was never called.
    assert runner.call_count == 0


def test_bypass_permissions_allowed_with_sandbox_wired(tmp_path):
    """`bypassPermissions` is permitted ONLY when a real sandbox_wrap is wired."""
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
            permission_mode="bypassPermissions",
            sandbox_wrap=bwrap_sandbox_wrap(str(clone)),
        )
    )
    assert env["status"] == "done"
    argv = runner.calls[0]["argv"]
    assert "bypassPermissions" in argv
    # The sandbox wrap injected --settings with the sandbox config.
    assert "--settings" in argv


def test_disallowed_tools_configurable(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
            disallowed_tools=["Bash"],
            allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"],
        )
    )
    argv = runner.calls[0]["argv"]
    di = argv.index("--disallowedTools")
    assert argv[di + 1] == "Bash"
    ai = argv.index("--allowedTools")
    assert argv[ai + 1 : ai + 6] == ["Read", "Edit", "Write", "Grep", "Glob"]


# ── BLOCKER (escalated): mandatory-sandbox gate for a REAL runner ───────────


class _FakeRealRunner(FakeCliRunner):
    """A fake that ADVERTISES it spawns a real process (so the sandbox gate
    applies) but spawns nothing — lets us test the gate with no real `claude`.

    Under the FAIL-CLOSED gate polarity (security #0), a runner is treated as
    real UNLESS it sets the fake marker, so this subclass must OVERRIDE the
    inherited ``no_real_process``/``is_fake`` exemption back to False to re-enter
    the gate. ``spawns_real_process = True`` is the explicit belt-and-suspenders
    positive signal (no longer load-bearing, but kept for documentation)."""

    spawns_real_process = True
    no_real_process = False
    is_fake = False


def test_real_runner_refused_unsandboxed_no_optout(tmp_path, monkeypatch):
    """A REAL runner with no sandbox + no opt-out is REFUSED before spawning."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = _FakeRealRunner(structured_output=_envelope())
    with pytest.raises(UnsandboxedRealRunError):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
            )
        )
    # Refused PRE-spawn.
    assert runner.call_count == 0


def test_real_runner_allowed_with_sandbox_wired(tmp_path, monkeypatch):
    """A REAL runner under a wired sandbox is allowed (the gate is satisfied)."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = _FakeRealRunner(structured_output=_envelope())
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
            sandbox_wrap=bwrap_sandbox_wrap(str(clone)),
        )
    )
    assert env["status"] == "done"
    assert runner.call_count == 1


def test_real_runner_allowed_with_explicit_optout(tmp_path, monkeypatch):
    """A REAL runner is allowed unsandboxed ONLY with the explicit operator
    opt-out (host already OS-confined)."""
    monkeypatch.setenv(UNSANDBOXED_OPT_OUT_ENV, "1")
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = _FakeRealRunner(structured_output=_envelope())
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["status"] == "done"


def test_fake_runner_is_exempt_from_sandbox_gate(tmp_path, monkeypatch):
    """The FakeCliRunner spawns no process → the sandbox gate does NOT apply, so
    the full unit-test suite runs without opt-out or a wired sandbox."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["status"] == "done"


def test_real_cli_runner_is_treated_as_real_fail_closed():
    """FAIL-CLOSED polarity (security #0): the real runner is treated as REAL by
    the gate, and it carries the belt-and-suspenders positive marker too."""
    # The gate decision is the load-bearing assertion.
    assert _runner_spawns_real_process(real_cli_runner) is True
    # The positive marker is retained as documentation / belt-and-suspenders.
    assert getattr(real_cli_runner, "spawns_real_process", False) is True


def test_unmarked_runner_is_treated_as_real_and_gated(tmp_path, monkeypatch):
    """An UNMARKED runner (no fake marker, no positive marker) FAILS CLOSED: the
    gate treats it as real and refuses it without a sandbox. This is the security
    #0 invariant — a real-spawning runner that FORGOT the marker is gated, not
    silently exempt."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)

    class _UnmarkedRunner:
        """Spawns nothing, but advertises NEITHER a fake marker NOR the positive
        ``spawns_real_process`` marker — simulating a forgotten real runner."""

        def __init__(self):
            self.calls: list[dict] = []

        @property
        def call_count(self) -> int:
            return len(self.calls)

        async def __call__(self, argv, cwd):
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 1},
                "is_error": False,
                "subtype": "success",
                "structured_output": _envelope(),
            }

    runner = _UnmarkedRunner()
    # The gate decision itself: an unmarked runner is real.
    assert _runner_spawns_real_process(runner) is True
    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(UnsandboxedRealRunError):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
            )
        )
    # Refused PRE-spawn — the forgotten-marker runner never ran.
    assert runner.call_count == 0


def test_explicit_spawns_real_false_does_not_exempt(tmp_path, monkeypatch):
    """A runner setting only ``spawns_real_process = False`` (a forgotten default,
    NOT an affirmative fake attestation) is still GATED — only an affirmative
    ``no_real_process``/``is_fake`` marker exempts."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)

    class _SpawnsRealFalseRunner:
        spawns_real_process = False  # forgotten default, NOT a fake attestation

        def __init__(self):
            self.calls: list[dict] = []

        @property
        def call_count(self) -> int:
            return len(self.calls)

        async def __call__(self, argv, cwd):
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return {
                "usage": {"output_tokens": 1},
                "is_error": False,
                "subtype": "success",
                "structured_output": _envelope(),
            }

    runner = _SpawnsRealFalseRunner()
    assert _runner_spawns_real_process(runner) is True
    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(UnsandboxedRealRunError):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
            )
        )
    assert runner.call_count == 0


def test_fake_marker_exempts_runner():
    """The affirmative fake markers (``no_real_process`` / ``is_fake``) are what
    exempt a runner — the gate decision is False for the FakeCliRunner."""
    assert _runner_spawns_real_process(FakeCliRunner()) is False


def test_journal_hit_skips_sandbox_gate(tmp_path, monkeypatch):
    """A $0 journal HIT spawns no process, so the sandbox gate must NOT fire even
    for a real runner with no sandbox — the cached envelope returns directly."""
    monkeypatch.delenv(UNSANDBOXED_OPT_OUT_ENV, raising=False)
    clone = tmp_path / "clone"
    clone.mkdir()
    journal = ResultJournal()
    # Pre-seed the journal with the exact key run_attempt will compute.
    key = journal.key(_task(), 1, model="sonnet", briefing="b", upstream_envelope_hashes=[])
    journal.put(key, _envelope(), usage={"output_tokens": 1})
    runner = _FakeRealRunner(structured_output=_envelope())
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=journal,
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=runner,
        )
    )
    assert env["status"] == "done"
    assert runner.call_count == 0  # journal hit — no spawn, gate not reached


# ── BLOCKER: minimal subprocess env (no secret leakage) ─────────────────────


def test_subprocess_env_drops_secrets_keeps_auth():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "USER": "u",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm",
        # secrets that MUST be dropped:
        "ANTHROPIC_API_KEY": "sk-should-not-leak",
        "GH_TOKEN": "ghp_should_not_leak",
        "GITHUB_TOKEN": "ghp_should_not_leak2",
        "AWS_SECRET_ACCESS_KEY": "aws-should-not-leak",
        "MY_RANDOM_SECRET": "nope",
    }
    env = build_subprocess_env(parent)
    # Auth-relevant vars survive (HOME carries subscription creds).
    assert env["HOME"] == "/home/u"
    assert env["PATH"] == "/usr/bin"
    assert env["LC_ALL"] == "C.UTF-8"
    # Secrets are gone.
    assert "ANTHROPIC_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "MY_RANDOM_SECRET" not in env


def test_subprocess_env_never_forwards_api_key_even_if_in_allowlist_path():
    """ANTHROPIC_API_KEY is on the explicit never-forward denylist — even though
    it would otherwise be a plausible 'credential' var, it is dropped so an
    autonomous agent never gets the key and the run never silently flips to API
    billing."""
    env = build_subprocess_env({"ANTHROPIC_API_KEY": "sk", "HOME": "/h", "PATH": "/b"})
    assert "ANTHROPIC_API_KEY" not in env


# ── BLOCKER: sandbox seam is fail-closed where it matters ───────────────────


def test_bwrap_sandbox_wrap_injects_failclosed_settings(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    wrap = bwrap_sandbox_wrap(str(clone))
    argv = wrap(["claude", "-p", "x"])
    assert "--settings" in argv
    import json

    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] is True
    # fail-closed: refuses to start when no OS sandbox is available.
    assert settings["sandbox"]["failIfUnavailable"] is True
    # writes confined to the clone (resolved).
    assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(clone.resolve())]


def test_unsandboxed_warning_fires_once(tmp_path, caplog):
    """The default (identity) sandbox wrap warns ONCE that we are unsandboxed."""
    import scripts.cli_dispatch as cd

    cd._UNSANDBOXED_WARNED = False  # reset the one-time latch for the test
    clone = tmp_path / "clone"
    clone.mkdir()
    runner = FakeCliRunner(structured_output=_envelope())
    with caplog.at_level("WARNING"):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=runner,
            )
        )
    assert any("UNSANDBOXED" in r.message for r in caplog.records)


# ── MAJOR: zombie child reaped on wall-clock timeout ────────────────────────


def test_real_runner_reaps_child_on_timeout():
    """A real subprocess (a long `sleep`) launched via the REAL runner is KILLED
    and reaped when the wall_clock trips — no orphan survives. Uses a dummy
    long-running command (no real `claude` needed)."""
    from scripts.cli_dispatch import real_cli_runner

    async def drive():
        # A 30s sleeper; wall_clock 0.2s trips first.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                real_cli_runner(["sleep", "30"], "/tmp"),  # nosec B108 — /tmp cwd for a dummy sleep
                timeout=0.2,
            )
        # Give the reap a beat to complete.
        await asyncio.sleep(0.2)

    asyncio.run(drive())

    # No orphaned `sleep 30` may survive (the runner killed + reaped it).
    import subprocess  # nosec B404 — test-only liveness check

    out = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
        ["pgrep", "-f", "sleep 30"], capture_output=True, text=True
    )
    assert out.stdout.strip() == "", f"orphaned sleep survived: {out.stdout!r}"


# ── MINOR 6: owned-loop FD hygiene (context manager + __del__) ──────────────


def test_owned_loop_closed_by_context_manager(tmp_path):
    """The owned loop is closed on context-manager exit — no ResourceWarning /
    unclosed-loop leak."""
    clone = tmp_path / "clone"
    clone.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        with CliDispatchTools(
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            clone_dir=str(clone),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=FakeCliRunner(structured_output=_envelope()),
        ) as tools:
            loop = tools._loop
            assert not loop.is_closed()
        # exited → owned loop closed.
        assert loop.is_closed()


def test_owned_loop_closed_by_del(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    tools = CliDispatchTools(
        budget=BudgetPool(total_tokens=100_000),
        journal=ResultJournal(),
        clone_dir=str(clone),
        model_for=lambda t, a: "sonnet",
        briefing_for=lambda t, a: "b",
        runner=FakeCliRunner(structured_output=_envelope()),
    )
    loop = tools._loop
    del tools
    gc.collect()
    assert loop.is_closed()


def test_supplied_loop_not_closed(tmp_path):
    """A caller-supplied loop is NOT closed by the tools (caller owns it)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    loop = asyncio.new_event_loop()
    try:
        tools = CliDispatchTools(
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            clone_dir=str(clone),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=FakeCliRunner(structured_output=_envelope()),
            loop=loop,
        )
        tools.close()
        assert not loop.is_closed()  # close() is a no-op for a supplied loop
    finally:
        loop.close()


# ── MINOR 7: stale-future guard ─────────────────────────────────────────────


def test_poll_refuses_stale_attempt_future(tmp_path):
    """Spawn attempt 1 (fails), re-spawn attempt 2; poll(task, 1) is None (stale
    attempt-1 future refused) and poll(task, 2) reads the new future."""
    clone = tmp_path / "clone"
    clone.mkdir()
    loop = asyncio.new_event_loop()
    try:
        runner = FakeCliRunner(is_error=True)  # attempt 1 → failed attempt
        tools = CliDispatchTools(
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            clone_dir=str(clone),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=runner,
            loop=loop,
        )
        task = _task()
        tools.spawn(task, 1)
        tools.pump()
        # attempt 1 failed → poll(.,1) is None (failed-attempt sentinel).
        assert tools.poll(task, 1) is None

        # Re-spawn attempt 2 with a SUCCEEDING runner.
        tools.runner = FakeCliRunner(structured_output=_envelope(attempt=2))
        tools.spawn(task, 2)
        tools.pump()
        # The stale attempt-1 poll must STILL be None (it is not the live future).
        assert tools.poll(task, 1) is None
        # The live attempt-2 poll reads the new future.
        result = tools.poll(task, 2)
        assert result is not None
        assert result["attempt"] == 2
    finally:
        loop.close()


def test_poll_failed_attempt_not_journaled_via_tools(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    loop = asyncio.new_event_loop()
    try:
        journal = ResultJournal()
        tools = CliDispatchTools(
            budget=BudgetPool(total_tokens=100_000),
            journal=journal,
            clone_dir=str(clone),
            model_for=lambda t, a: "sonnet",
            briefing_for=lambda t, a: "b",
            runner=FakeCliRunner(is_error=True),
            loop=loop,
        )
        task = _task()
        tools.spawn(task, 1)
        tools.pump()
        assert tools.poll(task, 1) is None
    finally:
        loop.close()


# ── MINOR 9: wall_clock invariant ───────────────────────────────────────────


def test_wall_clock_must_not_exceed_engine_deadline(tmp_path):
    """The adapter wall_clock_s must be <= the engine WALL_CLOCK_S; a longer
    subprocess deadline would defeat the termination proof's per-attempt bound."""
    from scripts.pm_dispatch import WALL_CLOCK_S

    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(AssertionError, match="wall_clock_s"):
        _run(
            run_attempt(
                _task(),
                1,
                budget=BudgetPool(total_tokens=100_000),
                journal=ResultJournal(),
                model="sonnet",
                briefing="b",
                clone_dir=str(clone),
                runner=FakeCliRunner(structured_output=_envelope()),
                wall_clock_s=WALL_CLOCK_S + 1.0,
            )
        )


def test_wall_clock_equal_to_engine_is_allowed(tmp_path):
    from scripts.cli_dispatch import is_failed_attempt as _ifa  # noqa: F401
    from scripts.pm_dispatch import WALL_CLOCK_S

    clone = tmp_path / "clone"
    clone.mkdir()
    env = _run(
        run_attempt(
            _task(),
            1,
            budget=BudgetPool(total_tokens=100_000),
            journal=ResultJournal(),
            model="sonnet",
            briefing="b",
            clone_dir=str(clone),
            runner=FakeCliRunner(structured_output=_envelope()),
            wall_clock_s=WALL_CLOCK_S,
        )
    )
    assert not is_failed_attempt(env)
    assert env["status"] == "done"
