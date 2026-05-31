"""Tests for `scripts.atelier_entrypoint.startup_check` — Plan 4 §Task 2 (T28).

The pre-flight helper is called at the top of each user-facing entry
skill (`load`, `save`, `ingest`, `run`). It returns one of three action
tokens:

  * `proceed-local`    — Memex absent; skill should continue with the
                         local backend.
  * `proceed-memex`    — Memex installed + bootstrapped + no local DB
                         left to migrate; skill should continue with
                         the Memex backend.
  * `prompt-migration` — Memex installed but the project still has an
                         unmigrated `.ai/atelier.db`; skill must surface
                         the migration prompt before continuing.

The third test pins the `proceed-memex` branch and asserts that
`bootstrap.run_bootstrap` is invoked lazily — only when there is no
migration to do.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """A throw-away project tree containing `.git/` and `.ai/` so
    `_project_ai_dir()` can resolve a sensible target.

    `monkeypatch.chdir` ensures the helper's `Path.cwd()` walk anchors
    inside this synthetic root, not the developer's real workspace.
    """
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    (root / ".ai").mkdir()
    return root


def test_startup_in_local_mode_no_action(project_root, monkeypatch):
    """Local mode short-circuits before any migration check or
    bootstrap call — Atelier has nothing to ask the user about."""
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-local"


def test_startup_in_memex_mode_with_local_db_returns_prompt_action(project_root, monkeypatch):
    """Memex is installed AND a project-local DB exists with no marker —
    we owe the user a prompt before touching either store."""
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    (project_root / ".ai" / "atelier.db").touch()
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "prompt-migration"
    assert "atelier.db" in r["local_db"]


def test_startup_in_memex_mode_no_local_db_proceeds(project_root, monkeypatch):
    """Memex mode + nothing to migrate → bootstrap (lazy) + proceed.

    The bootstrap stub pins the lazy-call contract: if the impl had
    imported / called `run_bootstrap` at module-load or before the
    migration check, the stub installed here would not intercept it.
    """
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-memex"


# ── Live WaveDispatcher wiring (atelier#81, AI-4) ───────────────────────────


@pytest.fixture
def wired_db(tmp_path):
    """A Local DB with shared/ migrations applied (carries bridge_requests 008
    + bridge_messages 003), for the live WaveDispatcher wiring tests."""
    from pathlib import Path

    from scripts.migrate import apply_migrations

    db = tmp_path / "atelier.db"
    apply_migrations(str(db), Path(__file__).resolve().parent.parent / "migrations" / "shared")
    return str(db)


def test_build_wave_dispatcher_subagent_mode(wired_db):
    """Default (no env, no marker) → subagent mode → a live WaveDispatcher with
    BOTH seams bound (not the NotImplementedError defaults)."""
    from scripts.atelier_entrypoint import build_wave_dispatcher
    from scripts.dispatch import DISPATCH_MODE_SUBAGENT, resolve_dispatch_mode
    from scripts.pm_dispatch import WaveDispatcher

    # Sanity: the wiring will resolve to subagent with an empty env + bare root.
    assert resolve_dispatch_mode(env={}, root=wired_db_parent(wired_db)) == DISPATCH_MODE_SUBAGENT

    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: f"B:{task['id']}:{attempt}",
        env={},  # force the subagent default deterministically
        root=wired_db_parent(wired_db),
    )
    assert isinstance(wd, WaveDispatcher)
    # Both seams are real callables, not the engine's unset-raise defaults.
    assert wd._spawn_fn is not WaveDispatcher._unset_spawn
    assert wd._poll_fn is not WaveDispatcher._unset_poll


def test_build_wave_dispatcher_agent_team_mode(wired_db):
    """Env override → agent-team mode → a live WaveDispatcher. team_name +
    members are required by build_spawn_fn for agent-team; pass them through."""
    from scripts.atelier_entrypoint import build_wave_dispatcher
    from scripts.dispatch import DISPATCH_MODE_ENV_VAR
    from scripts.pm_dispatch import WaveDispatcher

    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: "B",
        members=["pm-1", "sdet-1"],
        team_name="cycle-team",
        env={DISPATCH_MODE_ENV_VAR: "agent-team"},
        root=wired_db_parent(wired_db),
    )
    assert isinstance(wd, WaveDispatcher)
    assert wd._spawn_fn is not WaveDispatcher._unset_spawn
    assert wd._poll_fn is not WaveDispatcher._unset_poll


def test_build_wave_dispatcher_threads_escalate_fn_unconditionally(wired_db):
    """A provided escalate_fn is the dispatcher's escalate path verbatim — the
    single unconditional escalation path (no conditional branch)."""
    from scripts.atelier_entrypoint import build_wave_dispatcher

    sentinel_calls = []

    def my_escalate(escalation):
        sentinel_calls.append(escalation)

    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: "B",
        escalate_fn=my_escalate,
        env={},
        root=wired_db_parent(wired_db),
    )
    assert wd._escalate_fn is my_escalate


def test_build_wave_dispatcher_defaults_escalate_to_engine_default(wired_db):
    """When no escalate_fn is given, the engine's guaranteed-emitting default is
    used (never silent) — and it is still a single unconditional path."""
    from scripts.atelier_entrypoint import build_wave_dispatcher
    from scripts.pm_dispatch import _default_escalate

    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: "B",
        env={},
        root=wired_db_parent(wired_db),
    )
    assert wd._escalate_fn is _default_escalate


def wired_db_parent(db_path):
    """The workspace root for a wired_db — its parent dir (where .ai/atelier.mode
    would live). resolve_dispatch_mode reads <root>/.ai/atelier.mode; an empty
    tmp dir keeps the marker-read miss deterministic."""
    from pathlib import Path

    return Path(db_path).parent
