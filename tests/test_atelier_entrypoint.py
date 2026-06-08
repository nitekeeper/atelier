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


# ── Resume detection wiring (atelier#66 [S2], AC3) ──────────────────────────


def test_startup_local_adds_resume_offer_when_arc_resumable(project_root, monkeypatch):
    """On the LOCAL branch, startup_check surfaces a `resume_offer` field
    ALONGSIDE the existing `proceed-local` action when find_resumable_arc returns
    an offer. The action contract is unchanged — resume_offer is additive.

    ANTI-REVERT: if the resume wiring is dropped, `resume_offer` is absent and
    this assertion goes RED. resume.find_resumable_arc is monkeypatched so this
    pins the WIRING without standing up a full aborted-arc DB fixture (that is
    exercised end-to-end in test_resume.py)."""
    from scripts import resume

    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    sentinel = resume.ResumeOffer(
        team_id="team-x",
        team_pk="run-x",
        project_id="proj-x",
        abort_phase="implement:in-progress",
        incomplete_count=3,
    )
    monkeypatch.setattr("scripts.resume.find_resumable_arc", lambda *a, **k: sentinel)
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-local"  # contract intact
    assert r["resume_offer"] is sentinel


def test_startup_local_no_resume_offer_when_none(project_root, monkeypatch):
    """When find_resumable_arc returns None, startup_check returns the bare
    proceed-local action with NO resume_offer key — a clean run is offered no
    resume prompt."""
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    monkeypatch.setattr("scripts.resume.find_resumable_arc", lambda *a, **k: None)
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-local"
    assert "resume_offer" not in r


def test_startup_memex_branch_does_not_consult_resume(project_root, monkeypatch):
    """Resume detection is LOCAL-only (§17): the Memex branch must NOT call
    find_resumable_arc (a Memex-mode run has no Local team dispatch state to
    resume). We make find_resumable_arc explode to prove it is never reached on
    the Memex path."""
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})

    def _boom(*a, **k):
        raise AssertionError("resume.find_resumable_arc must not run on the Memex branch")

    monkeypatch.setattr("scripts.resume.find_resumable_arc", _boom)
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-memex"
    assert "resume_offer" not in r


# ── Settings-recommendation offer wiring (atelier settings-rec, AI-2) ───────
#
# The version-upgrade settings offer is MODE-AGNOSTIC: it attaches a
# `settings_rec_offer` field on BOTH the proceed-local AND the proceed-memex
# return (a plugin bump is mode-independent — unlike the Local-only
# resume_offer). recommended_settings.maybe_offer is monkeypatched so these pin
# the WIRING (both branches), not the eligibility logic (covered in
# test_recommended_settings.py).


def test_startup_local_attaches_settings_rec_offer(project_root, monkeypatch):
    """LOCAL branch: a non-None maybe_offer() is attached as settings_rec_offer
    alongside the unchanged proceed-local action."""
    from scripts import recommended_settings

    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    monkeypatch.setattr("scripts.resume.find_resumable_arc", lambda *a, **k: None)
    sentinel = {"eligible": True, "current_version": "1.5.0", "changes": {"model": "sonnet"}}
    monkeypatch.setattr(recommended_settings, "maybe_offer", lambda: sentinel)
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-local"  # contract intact
    assert r["settings_rec_offer"] is sentinel


def test_startup_memex_attaches_settings_rec_offer(project_root, monkeypatch):
    """MEMEX branch (no local DB to migrate): the SAME offer attaches on
    proceed-memex. ANTI-REVERT (the Local-only-mirror trap): if the wiring is
    attached on ONLY one branch, one of these two tests goes RED."""
    from scripts import recommended_settings

    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})
    sentinel = {"eligible": True, "current_version": "1.5.0", "changes": {"model": "sonnet"}}
    monkeypatch.setattr(recommended_settings, "maybe_offer", lambda: sentinel)
    from scripts.atelier_entrypoint import startup_check

    r = startup_check()
    assert r["action"] == "proceed-memex"
    assert r["settings_rec_offer"] is sentinel


def test_startup_no_settings_rec_offer_when_none(project_root, monkeypatch):
    """maybe_offer() == None → the settings_rec_offer key is ABSENT under BOTH
    modes (clean, already-applied, or declined-this-version run)."""
    from scripts import recommended_settings

    monkeypatch.setattr(recommended_settings, "maybe_offer", lambda: None)

    # Local
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    monkeypatch.setattr("scripts.resume.find_resumable_arc", lambda *a, **k: None)
    from scripts.atelier_entrypoint import startup_check

    r_local = startup_check()
    assert r_local["action"] == "proceed-local"
    assert "settings_rec_offer" not in r_local

    # Memex
    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})
    r_memex = startup_check()
    assert r_memex["action"] == "proceed-memex"
    assert "settings_rec_offer" not in r_memex


def test_startup_settings_rec_offer_error_is_swallowed(project_root, monkeypatch):
    """A raising maybe_offer() degrades to a no-op — startup_check still returns
    the bare action (no crash) under both modes."""
    from scripts import recommended_settings

    def _boom():
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr(recommended_settings, "maybe_offer", _boom)

    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "local")
    monkeypatch.setattr("scripts.resume.find_resumable_arc", lambda *a, **k: None)
    from scripts.atelier_entrypoint import startup_check

    r_local = startup_check()
    assert r_local["action"] == "proceed-local"
    assert "settings_rec_offer" not in r_local

    monkeypatch.setattr("scripts.mode_detector.detect_mode", lambda: "memex")
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap", lambda: {"version": "1.1.0"})
    r_memex = startup_check()
    assert r_memex["action"] == "proceed-memex"
    assert "settings_rec_offer" not in r_memex


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


# ── model-tier seam threading through build_wave_dispatcher (atelier) ────────
#
# The lower-level #81 factory leaves model_for UNSET by default (None → no model
# attached to any spawn — byte-identical to pre-policy). When a model_for is
# injected it threads straight into build_spawn_fn → the chosen tier lands in the
# enqueued args_json.

import json as _mt_json  # noqa: E402
import sqlite3 as _mt_sqlite3  # noqa: E402


def _mt_spawn_args(db_path, kind):
    con = _mt_sqlite3.connect(db_path)
    con.row_factory = _mt_sqlite3.Row
    try:
        rows = con.execute(
            "SELECT args_json FROM bridge_requests WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
        return [_mt_json.loads(r["args_json"]) for r in rows]
    finally:
        con.close()


def test_build_wave_dispatcher_no_model_for_omits_model_key(wired_db, monkeypatch):
    """BACK-COMPAT: build_wave_dispatcher with NO model_for => spawn args_json has
    NO "model" key (byte-identical to the pre-policy row shape)."""
    from scripts import mode_detector
    from scripts.atelier_entrypoint import build_wave_dispatcher

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: "B",
        env={},  # subagent default
        root=wired_db_parent(wired_db),
    )
    wd._spawn_fn({"id": 1}, 1)
    args = _mt_spawn_args(wired_db, "spawn_subagent")
    assert len(args) == 1
    assert "model" not in args[0]


def test_build_wave_dispatcher_threads_injected_model_for(wired_db, monkeypatch):
    """An injected model_for threads into build_spawn_fn — the chosen tier lands
    in args_json."""
    from scripts import mode_detector
    from scripts.atelier_entrypoint import build_wave_dispatcher

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    wd = build_wave_dispatcher(
        wired_db,
        team_pk="cycle-1",
        team_id="T",
        briefing_for=lambda task, attempt: "B",
        model_for=lambda task, attempt: "opus",
        env={},
        root=wired_db_parent(wired_db),
    )
    wd._spawn_fn({"id": 1}, 1)
    args = _mt_spawn_args(wired_db, "spawn_subagent")
    assert args[0]["model"] == "opus"
