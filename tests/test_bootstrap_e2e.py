"""End-to-end bootstrap tests for `scripts/bootstrap.py`.

Covers Plan 2 Task 10:
  - `run_bootstrap()` dispatches by `mode_detector.detect_mode()`.
  - `_require_memex_version()` enforces the v2.2.0 API floor.
  - Memex-mode path seeds roles + agents + registers the atelier store,
    idempotently.
  - Local-mode path creates `.ai/atelier.db`, applies both migration
    bundles (shared + local-only), and seeds roles + agents.
  - `MemexNotInitializedError` is reformatted into a clean RuntimeError.
  - Marker version-skip short-circuits a second invocation in O(1).
  - `_memex_scripts_context` restores `sys.modules["scripts"]` on both
    normal exit and exception.

Hermetic — the Memex fixture under `tests/fixtures/memex_min/` is the
trimmed copy of the real Memex install needed to exercise the bootstrap
path. CI never reaches outside the repo.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


# Hermetic Memex fixture — vendored stub under tests/fixtures/memex_min/.
# Trimmed so it exposes only what bootstrap.py actually needs (scripts.db,
# scripts.registry, scripts.stores, scripts.roles, scripts.agents) plus
# the db/agents.sql + db/migrations_table.sql files. See
# tests/fixtures/memex_min/README equivalent in scripts/__init__.py
# docstrings.
_MEMEX_MIN = Path(__file__).resolve().parent / "fixtures" / "memex_min"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _copy_memex_min_into(plugin_root: Path, *, version: str = "2.5.1") -> None:
    """Copy the hermetic memex_min stub into `plugin_root`, overriding the
    manifest version if requested.

    The bootstrap path needs `scripts.db`, `scripts.registry`,
    `scripts.stores`, `scripts.agents`, `scripts.roles` modules to import,
    plus the `db/agents.sql` + `db/migrations_table.sql` files. We don't
    run install — we just hand-seed `agents.db` + `registry.json` so the
    bootstrap target is reachable.
    """
    if not _MEMEX_MIN.is_dir():
        pytest.fail(
            f"hermetic memex_min fixture missing at {_MEMEX_MIN}. "
            "The fixture is required for bootstrap E2E tests."
        )
    plugin_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_MEMEX_MIN / "scripts", plugin_root / "scripts")
    shutil.copytree(_MEMEX_MIN / "db", plugin_root / "db")
    (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": version})
    )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a tmp directory so everything that reads
    `~/.memex/...` ends up under tmp_path.

    Both `MEMEX_HOME` and the `Path.home` monkeypatch point at the same
    tmp_path so atelier-side reads (Path.home/.memex) and memex-stub-side
    reads (`memex_home()` which honors `$MEMEX_HOME`) agree.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MEMEX_HOME", str(home / ".memex"))
    return {"home": home}


@pytest.fixture
def memex_install(tmp_path, fake_home, monkeypatch):
    """Stand up a Memex install whose `~/.memex/` lives under fake_home
    and whose plugin scripts live in `tmp_path / "memex_plugin"`.

    Mode detection will report "memex" once the registry + config are in
    place. The bootstrap recipe then runs against this install.
    """
    plugin_root = tmp_path / "memex_plugin"
    _copy_memex_min_into(plugin_root)

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


def _expected_role_count() -> int:
    """Pin the seed shape — count is whatever `seed_data.load_role_seed()`
    reports, not a hardcoded literal that drifts when seeds change."""
    from scripts import seed_data
    return len(seed_data.load_role_seed())


def _expected_agent_count() -> int:
    from scripts import seed_data
    return len(seed_data.load_agent_seed())


# ── Memex-mode tests ──────────────────────────────────────────────────────────


def test_bootstrap_seeds_roles_agents_and_creates_store(memex_install):
    """Fresh state: bootstrap seeds the full role+agent catalog into
    agents.db and registers the atelier store via memex:core:create-store.
    Also asserts the marker payload carries `mode == "memex"`, the
    matching atelier version, and `memex_version`.
    """
    from scripts.bootstrap import run_bootstrap, _atelier_version
    result = run_bootstrap()

    assert result["mode"] == "memex"

    # Roles: every role from the role seed should now be in agents.db.
    from scripts import seed_data
    expected_roles = {r["name"] for r in seed_data.load_role_seed()}
    conn = sqlite3.connect(memex_install["agents_db"])
    conn.row_factory = sqlite3.Row
    seeded_roles = {r["name"] for r in conn.execute("SELECT name FROM roles")}
    assert expected_roles <= seeded_roles
    assert len(seeded_roles) == _expected_role_count()

    # Agents: every agent_id from the agent seed should now be in agents.db.
    expected_agents = {a["agent_id"] for a in seed_data.load_agent_seed()}
    seeded_agents = {r["id"] for r in conn.execute("SELECT id FROM agents")}
    conn.close()
    assert expected_agents <= seeded_agents
    assert len(seeded_agents) == _expected_agent_count()

    # registry.json is a flat map per memex/scripts/registry.py.
    registry = json.loads(
        (memex_install["memex_home"] / "registry.json").read_text()
    )
    assert "atelier" in registry  # flat map; no nested "stores" key
    # The atelier.db must exist where the registry record points.
    atelier_db_path = Path(registry["atelier"]["path"])
    assert atelier_db_path.exists()

    # Marker payload assertions (N3).
    marker = json.loads(
        (memex_install["memex_home"] / "atelier.bootstrap.json").read_text()
    )
    assert marker["mode"] == "memex"
    assert marker["version"] == _atelier_version()
    assert marker.get("memex_version")


def test_bootstrap_is_idempotent(memex_install):
    """Second invocation must NOT duplicate roles, agents, or stores.

    Uses `force=True` so the marker-skip optimization doesn't short-circuit
    the second invocation — we want to actually exercise the body's
    pre-check guards.
    """
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

    # Second invocation — force=True bypasses the marker-skip so we
    # exercise the actual body's idempotency, not the marker shortcut.
    run_bootstrap(force=True)

    conn = sqlite3.connect(memex_install["agents_db"])
    role_count_2 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_2 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    registry_2 = json.loads(
        (memex_install["memex_home"] / "registry.json").read_text()
    )

    assert role_count_1 == role_count_2
    assert agent_count_1 == agent_count_2
    # Full structural equality — not just key sets (N5). Any drift in
    # the registry record (e.g. a registered_at timestamp re-written, a
    # path mutated) is a regression we want to catch.
    assert registry_1 == registry_2


def test_bootstrap_skips_when_marker_matches_version(memex_install,
                                                     monkeypatch):
    """Second invocation MUST short-circuit before seeding when the
    marker's `version` matches the running atelier version (spec §5
    step 1 marker-skip optimization).

    We run bootstrap once to plant the marker, then patch the inner
    seed functions so a second invocation would explode if it reached
    them. If `run_bootstrap` honors the marker, those patches never
    fire and the test passes.
    """
    from scripts import bootstrap
    bootstrap.run_bootstrap()

    seed_calls: list[str] = []
    def _explode_roles(*_a, **_kw):  # pragma: no cover - must not be called
        seed_calls.append("roles")
        raise AssertionError("_seed_roles_memex was called on second run")
    def _explode_agents(*_a, **_kw):  # pragma: no cover - must not be called
        seed_calls.append("agents")
        raise AssertionError("_seed_agents_memex was called on second run")
    monkeypatch.setattr(bootstrap, "_seed_roles_memex", _explode_roles)
    monkeypatch.setattr(bootstrap, "_seed_agents_memex", _explode_agents)

    result = bootstrap.run_bootstrap()
    assert seed_calls == []
    assert result["mode"] == "memex"
    assert result["version"] == bootstrap._atelier_version()

    # force=True bypasses the skip — confirm the seeders ARE called when
    # we ask for a forced re-run.
    seed_calls.clear()
    with pytest.raises(AssertionError):
        bootstrap.run_bootstrap(force=True)
    assert "roles" in seed_calls  # the seeder DID fire


def test_run_bootstrap_rejects_old_memex_via_full_path(memex_install):
    """`run_bootstrap()` itself MUST refuse to run against Memex < v2.2.0,
    not just `_require_memex_version()`. The check is invoked BEFORE
    `detect_mode()` (which would silently downgrade to local).

    Reading the manifest directly here keeps "memex pinned but unusable"
    from leaking through as a quiet local-mode bootstrap.
    """
    plugin_root = memex_install["plugin_root"]
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "memex", "version": "2.1.0"}))

    # Clear caches so the new manifest version is read.
    from scripts import mode_detector, backend_memex
    mode_detector._clear_cache()
    backend_memex._load_memex_module.cache_clear()

    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match=r"requires Memex v2\.2\.0"):
        run_bootstrap()


def test_require_memex_version_rejects_old_memex(memex_install):
    """Direct unit-level coverage of `_require_memex_version`.

    Reads the version from the manifest pinned in `~/.memex/config.json`
    (Plan 1 F2 / F1 contract), NOT by lex-sort of the Claude Code plugin
    cache.
    """
    plugin_root = memex_install["plugin_root"]
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "memex", "version": "2.1.0"}))

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
    _copy_memex_min_into(plugin_root)
    (memex_home / "config.json").write_text(json.dumps({
        "plugin_root": str(plugin_root),
    }))
    # Note: no registry.json — _refuse_half_installed_memex will raise.

    from scripts import mode_detector, backend_memex
    mode_detector._clear_cache()
    backend_memex._load_memex_module.cache_clear()

    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match=r"Memex is not bootstrapped"):
        run_bootstrap()


def test_inner_memex_not_initialized_catch_reformats(memex_install,
                                                     monkeypatch):
    """Defense-in-depth: even if the outer `_refuse_half_installed_memex`
    guard is somehow bypassed (caller mocked it out, or a future
    refactor moves it), the inner try/except inside `_run_bootstrap_memex`
    MUST catch `MemexNotInitializedError` and reformat it with operator
    guidance.

    We neutralize the outer guard AND force `detect_mode` to return
    "memex" (its real implementation downgrades to "local" when
    registry.json is missing, which would route around the inner catch).
    Then we delete registry.json so memex's `require_bootstrap` raises,
    and assert the user-facing message is the clean RuntimeError (not
    the raw MemexNotInitializedError).
    """
    from scripts import bootstrap, mode_detector, backend_memex

    # Disable the outer half-installed guard so we reach the inner catch.
    monkeypatch.setattr(bootstrap, "_refuse_half_installed_memex",
                        lambda: None)
    # Force memex-mode dispatch even though registry.json is about to vanish.
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    # Delete registry.json AFTER memex_install set it up.
    (memex_install["memex_home"] / "registry.json").unlink()

    # Clear caches so the changed disk state is observed.
    backend_memex._load_memex_module.cache_clear()

    with pytest.raises(RuntimeError, match=r"Memex is not bootstrapped"):
        bootstrap.run_bootstrap()


# ── _memex_scripts_context state-restore tests (QA I1) ────────────────────────


def test_memex_scripts_context_restores_state_on_normal_exit(memex_install):
    """The context manager swaps `sys.modules["scripts"]` to the Memex
    package on entry and MUST restore Atelier's package on normal exit.
    """
    from scripts import bootstrap

    saved_before = sys.modules.get("scripts")
    plugin_root = memex_install["plugin_root"]

    with bootstrap._memex_scripts_context(plugin_root) as memex_pkg:
        # Inside the block: scripts resolves to the memex package.
        assert sys.modules["scripts"] is memex_pkg
        # And the memex package's file IS the fixture's __init__.py.
        assert Path(memex_pkg.__file__).is_relative_to(plugin_root)

    # After exit: scripts is restored to whatever it was before.
    assert sys.modules.get("scripts") is saved_before


def test_memex_scripts_context_restores_state_on_exception(memex_install):
    """If the body of the `with` block raises, state restoration MUST
    still happen — the swap is in a try/finally so the failure path
    still rolls back.
    """
    from scripts import bootstrap

    saved_before = sys.modules.get("scripts")
    plugin_root = memex_install["plugin_root"]

    with pytest.raises(RuntimeError, match="boom"):
        with bootstrap._memex_scripts_context(plugin_root):
            # Confirm the swap happened before the raise.
            assert sys.modules["scripts"] is not saved_before
            raise RuntimeError("boom")

    assert sys.modules.get("scripts") is saved_before


# ── Local-mode tests ──────────────────────────────────────────────────────────


def test_bootstrap_local_mode_creates_atelier_db(local_workspace):
    """Fresh local-mode workspace: bootstrap must create
    `<workspace>/.ai/atelier.db` with both shared/ and local-only/
    migrations applied (workspaces, projects, project_documents, tasks,
    meeting_minutes, sessions, phases, plus roles + agents).

    Also asserts the marker payload carries `mode == "local"` and the
    matching atelier version.
    """
    from scripts.bootstrap import run_bootstrap, _atelier_version
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

    # Marker payload (N3) — local mode pins mode + version, no memex_version.
    marker = json.loads(
        (local_workspace / ".ai" / "atelier.bootstrap.json").read_text()
    )
    assert marker["mode"] == "local"
    assert marker["version"] == _atelier_version()
    assert "memex_version" not in marker


def test_bootstrap_local_mode_seeds_roles_in_local_db(local_workspace):
    """The full role/agent catalog must land in the local roles/agents
    tables. Idempotency is exercised by re-running bootstrap with
    `force=True` (bypassing the marker-skip) and verifying the counts
    didn't change.
    """
    from scripts.bootstrap import run_bootstrap
    run_bootstrap()

    db = local_workspace / ".ai" / "atelier.db"
    conn = sqlite3.connect(str(db))
    role_count_1 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_1 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    assert role_count_1 == _expected_role_count()
    assert agent_count_1 == _expected_agent_count()

    # Idempotency: second invocation (forced past the marker-skip) must
    # not duplicate.
    run_bootstrap(force=True)
    conn = sqlite3.connect(str(db))
    role_count_2 = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    agent_count_2 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    assert role_count_2 == role_count_1
    assert agent_count_2 == agent_count_1
