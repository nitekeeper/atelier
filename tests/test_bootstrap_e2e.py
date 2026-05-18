"""End-to-end bootstrap tests for `scripts/bootstrap.py`.

Covers Plan 2 Task 10:
  - `run_bootstrap()` dispatches by `mode_detector.detect_mode()`.
  - `_require_memex_version()` enforces the v2.2.0 API floor.
  - Memex-mode path seeds roles + agents + registers the atelier store,
    idempotently.
  - Local-mode path creates `.ai/atelier.db`, applies both migration
    bundles (shared + local-only), and seeds roles + agents.
  - `MemexNotInitializedError` is reformatted into a clean RuntimeError.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _find_real_memex() -> Path | None:
    """Locate the host's real Memex install (must be called BEFORE any
    Path.home monkeypatching so we still see the developer's real home).
    """
    import os
    real_home = Path(os.path.expanduser("~"))
    candidates = [
        real_home / "apps" / "memex",
        real_home / "Documents" / "Skills" / "memex",
    ]
    return next((p for p in candidates if (p / "scripts").is_dir()), None)


def _copy_real_memex_into(plugin_root: Path, *, version: str = "2.5.1",
                          real_memex: Path | None = None) -> None:
    """Copy a real Memex install's scripts/ + db/ into `plugin_root`.

    The bootstrap path needs real `scripts.db`, `scripts.registry`,
    `scripts.stores`, `scripts.agents`, `scripts.roles` modules to import,
    plus the `db/agents.sql` schema file `scripts.install._seed_internal`
    would normally have applied. We don't run install — we just hand-seed
    `agents.db` + `registry.json` so the bootstrap target is reachable.

    `real_memex` SHOULD be discovered via `_find_real_memex()` before any
    fixture monkeypatches `Path.home`.
    """
    if real_memex is None:
        real_memex = _find_real_memex()
    if real_memex is None:
        pytest.skip("real Memex repo not available")

    (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": version})
    )
    shutil.copytree(real_memex / "scripts", plugin_root / "scripts")
    if (real_memex / "db").is_dir():
        shutil.copytree(real_memex / "db", plugin_root / "db")
    # scripts.install.py touches scripts.paths.PLUGIN_ROOT for the
    # MemexNotInitializedError message — make sure that import resolves
    # by pinning the package root in a way that the loaded module can
    # discover. The Memex `scripts.paths` module computes PLUGIN_ROOT
    # from `__file__`, so copying scripts/ in place is sufficient.


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a tmp directory so everything that reads
    `~/.memex/...` (mode_detector, backend_memex._memex_plugin_root,
    memex_db.memex_home) ends up under tmp_path.

    Both env vars (`MEMEX_HOME` for the real memex package, plus the
    Path.home monkeypatch for atelier-side reads) point at the same
    tmp_path so the two halves of the bootstrap recipe agree on home.

    We snapshot the real memex location BEFORE the patch lands so the
    memex_install fixture can find the bundled scripts to copy.
    """
    real_memex = _find_real_memex()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Also expose MEMEX_HOME explicitly — Memex v2.5.0+ honors it ahead
    # of the $HOME validation in memex_home().
    monkeypatch.setenv("MEMEX_HOME", str(home / ".memex"))
    return {"home": home, "real_memex": real_memex}


@pytest.fixture
def memex_install(tmp_path, fake_home, monkeypatch):
    """Stand up a Memex install whose `~/.memex/` lives under fake_home
    and whose plugin scripts live in `tmp_path / "memex_plugin"`.

    Mode detection will report "memex" once the registry + config are in
    place. The bootstrap recipe then runs against this install.
    """
    plugin_root = tmp_path / "memex_plugin"
    _copy_real_memex_into(plugin_root, real_memex=fake_home["real_memex"])

    memex_home = fake_home["home"] / ".memex"
    memex_home.mkdir()
    # registry.json must exist for memex_db.require_bootstrap to pass.
    (memex_home / "registry.json").write_text("{}")
    (memex_home / "config.json").write_text(json.dumps({
        "plugin_root": str(plugin_root),
    }))

    # Seed agents.db with Memex's own schema so role/agent inserts have
    # tables to land in. Plain sqlite3 — no need to import the memex
    # package here; the SQL file is plain.
    agents_db = memex_home / "agents.db"
    schema_sql = (plugin_root / "db" / "agents.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(str(agents_db))
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

    # Reset mode_detector cache so each test re-evaluates.
    from scripts import mode_detector
    mode_detector._clear_cache()

    # Clear backend_memex's lru_cache for module loads so each test gets a
    # fresh plugin import.
    from scripts import backend_memex
    backend_memex._load_memex_module.cache_clear()

    return {
        "home": fake_home["home"],
        "memex_home": memex_home,
        "plugin_root": plugin_root,
        "agents_db": str(agents_db),
    }


@pytest.fixture
def local_workspace(tmp_path, fake_home, monkeypatch):
    """Set up a fresh local-mode workspace with a git root but no Memex
    install. mode_detector.detect_mode() should return "local"."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    monkeypatch.chdir(workspace)

    from scripts import mode_detector
    mode_detector._clear_cache()
    return workspace


# ── Memex-mode tests ──────────────────────────────────────────────────────────


def test_bootstrap_seeds_roles_agents_and_creates_store(memex_install):
    """Fresh state: bootstrap seeds 61 roles + 61 agents into agents.db
    and registers the atelier store via memex:core:create-store."""
    from scripts.bootstrap import run_bootstrap
    result = run_bootstrap()

    assert result["mode"] == "memex"

    # Roles: every role from templates/roles.json should now be in agents.db.
    from scripts import seed_data
    expected_roles = {r["name"] for r in seed_data.load_role_seed()}
    conn = sqlite3.connect(memex_install["agents_db"])
    conn.row_factory = sqlite3.Row
    seeded_roles = {r["name"] for r in conn.execute("SELECT name FROM roles")}
    assert expected_roles <= seeded_roles
    # Sanity: 61 expected, 61 seeded.
    assert len(seeded_roles) >= 61

    # Agents: every agent_id from templates/agents/*.json should now be
    # in agents.db.
    expected_agents = {a["agent_id"] for a in seed_data.load_agent_seed()}
    seeded_agents = {r["id"] for r in conn.execute("SELECT id FROM agents")}
    conn.close()
    assert expected_agents <= seeded_agents
    assert len(seeded_agents) >= 61

    # registry.json is a flat map per memex/scripts/registry.py.
    registry = json.loads(
        (memex_install["memex_home"] / "registry.json").read_text()
    )
    assert "atelier" in registry  # flat map; no nested "stores" key
    # The atelier.db must exist where the registry record points.
    atelier_db_path = Path(registry["atelier"]["path"])
    assert atelier_db_path.exists()


def test_bootstrap_is_idempotent(memex_install):
    """Second invocation must NOT duplicate roles, agents, or stores."""
    from scripts.bootstrap import run_bootstrap

    run_bootstrap()

    # Snapshot counts after first run.
    conn = sqlite3.connect(memex_install["agents_db"])
    role_count_1 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_1 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    registry_1 = json.loads(
        (memex_install["memex_home"] / "registry.json").read_text()
    )

    # Second invocation — must be safe.
    run_bootstrap()

    conn = sqlite3.connect(memex_install["agents_db"])
    role_count_2 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_2 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    registry_2 = json.loads(
        (memex_install["memex_home"] / "registry.json").read_text()
    )

    assert role_count_1 == role_count_2
    assert agent_count_1 == agent_count_2
    # Registry should be unchanged (atelier store registered exactly once).
    assert set(registry_1.keys()) == set(registry_2.keys())


def test_bootstrap_rejects_old_memex(memex_install):
    """Bootstrap MUST refuse to run against Memex < v2.2.0 because the
    caller-built librarian_output contract isn't there.

    The version check reads from the plugin manifest at the path pinned
    in `~/.memex/config.json` (Plan 1 F2 / F1 contract), NOT by lex-sort
    of the Claude Code plugin cache.
    """
    plugin_root = memex_install["plugin_root"]
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "memex", "version": "2.1.0"}))

    # mode_detector cache may have been populated by previous calls; the
    # detector returns "local" for under-floor versions, so we still need
    # to test the bootstrap module's own check directly.
    from scripts import bootstrap
    with pytest.raises(RuntimeError, match=r"requires Memex v2\.2\.0"):
        bootstrap._require_memex_version()


def test_bootstrap_fails_when_memex_not_initialized(tmp_path, fake_home,
                                                     monkeypatch):
    """If Memex itself isn't bootstrapped (no registry.json), atelier
    bootstrap must fail fast with operator guidance — not partway
    through with a confusing sqlite or file-missing error.
    """
    # No registry.json — only a config.json pointing at a manifest.
    memex_home = fake_home["home"] / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "plugin"
    _copy_real_memex_into(plugin_root, real_memex=fake_home["real_memex"])
    (memex_home / "config.json").write_text(json.dumps({
        "plugin_root": str(plugin_root),
    }))
    # Note: no registry.json — memex_db.require_bootstrap() will raise.

    from scripts import mode_detector, backend_memex
    mode_detector._clear_cache()
    backend_memex._load_memex_module.cache_clear()

    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match=r"Memex is not bootstrapped"):
        run_bootstrap()


# ── Local-mode tests ──────────────────────────────────────────────────────────


def test_bootstrap_local_mode_creates_atelier_db(local_workspace):
    """Fresh local-mode workspace: bootstrap must create
    `<workspace>/.ai/atelier.db` with both shared/ and local-only/
    migrations applied (workspaces, projects, project_documents, tasks,
    meeting_minutes, sessions, phases, plus roles + agents)."""
    from scripts.bootstrap import run_bootstrap
    result = run_bootstrap()

    assert result["mode"] == "local"

    db = local_workspace / ".ai" / "atelier.db"
    assert db.exists()

    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    # shared/ schema (subset)
    assert {"workspaces", "projects", "project_documents", "tasks",
            "meeting_minutes", "sessions", "phases", "phase_bypasses",
            "skill_gates"} <= tables
    # local-only/ schema
    assert {"roles", "agents"} <= tables


def test_bootstrap_local_mode_seeds_roles_in_local_db(local_workspace):
    """61 roles + 61 agents must land in the local roles/agents tables.

    Idempotency is exercised by re-running bootstrap and verifying the
    counts didn't change.
    """
    from scripts.bootstrap import run_bootstrap
    run_bootstrap()

    db = local_workspace / ".ai" / "atelier.db"
    conn = sqlite3.connect(str(db))
    role_count_1 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_1 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    assert role_count_1 == 61
    assert agent_count_1 == 61

    # Idempotency: second invocation must not duplicate.
    run_bootstrap()
    conn = sqlite3.connect(str(db))
    role_count_2 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_2 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    assert role_count_2 == role_count_1
    assert agent_count_2 == agent_count_1
