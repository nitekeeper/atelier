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
