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


def test_startup_in_memex_mode_with_local_db_returns_prompt_action(
        project_root, monkeypatch):
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
    monkeypatch.setattr("scripts.bootstrap.run_bootstrap",
                        lambda: {"version": "1.1.0"})
    from scripts.atelier_entrypoint import startup_check
    r = startup_check()
    assert r["action"] == "proceed-memex"
