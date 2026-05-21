"""Regression: atelier bootstrap must call memex.ensure_internal_agents()
after seeding atelier's roster into ~/.memex/agents.db.

Bug class — issue #9 (companion to memex PR #20):
    Atelier's `_run_bootstrap_memex` writes atelier's 61-role roster
    directly into memex's `~/.memex/agents.db` via memex CRUD. Memex
    itself owns 5 *internal* agents (`librarian-1`, `reference-librarian-1`,
    `archivist-1`, `dba-1`, `data-steward-1`) that other writers can leave
    missing — for example if the agents.db was created without going
    through memex's own install path. Prior to the fix, nothing told
    memex to re-verify its own invariants after atelier's seeding, so
    these 5 agents could silently stay missing on a live machine until
    the next `memex.install.run()`.

Fix (in atelier): after seeding the atelier roster, atelier's bootstrap
calls `memex.scripts.install.ensure_internal_agents(agents_db_path)`.
Soft-import: if memex < 2.6.0 (no public API yet), atelier logs and
continues — preserves backward compatibility.

This test exercises the wiring by planting a `scripts.install` shim into
the hermetic memex_min plugin fixture. The shim's `ensure_internal_agents`
records every call and seeds the 5 internal agents into agents.db. The
test then asserts:
  - The shim was invoked with the path to `~/.memex/agents.db`.
  - All 5 internal Memex agents are present in agents.db after bootstrap.

The companion test `test_bootstrap_soft_import_missing_install_module`
covers the < 2.6.0 path: no `scripts.install` module on disk → bootstrap
must NOT crash.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

# Reuse the hermetic fixture builder from test_bootstrap_e2e.
_MEMEX_MIN = Path(__file__).resolve().parent / "fixtures" / "memex_min"

INTERNAL_MEMEX_AGENT_IDS = (
    "archivist-1",
    "data-steward-1",
    "dba-1",
    "librarian-1",
    "reference-librarian-1",
)


def _copy_memex_min_into(plugin_root: Path, *, version: str = "2.6.0") -> None:
    """Same hermetic stub setup as test_bootstrap_e2e, but defaults to
    a memex version >= 2.6.0 since this regression is about the >= 2.6.0
    API contract. v2.6.0 == ensure_internal_agents available.
    """
    if not _MEMEX_MIN.is_dir():
        pytest.fail(f"hermetic memex_min fixture missing at {_MEMEX_MIN}.")
    plugin_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_MEMEX_MIN / "scripts", plugin_root / "scripts")
    shutil.copytree(_MEMEX_MIN / "db", plugin_root / "db")
    (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": version})
    )


def _write_ensure_internal_agents_shim(plugin_root: Path, *, call_log: Path) -> None:
    """Plant a `scripts/install.py` inside the hermetic memex plugin
    that exposes `ensure_internal_agents(db_path)`. The shim:

      - Appends `db_path` to `call_log` so the test can assert it ran.
      - Seeds the 5 internal Memex agents into the named agents.db.
      - Returns the same dict shape as the real memex API.

    This is the >= 2.6.0 happy path. By placing the file inside the
    memex plugin's `scripts/` dir, atelier's `_memex_scripts_context`
    (which swaps `sys.modules["scripts"]` to memex's package) makes
    `from scripts.install import ensure_internal_agents` resolve here.
    """
    shim = f'''
"""Hermetic stub of memex.scripts.install for atelier bootstrap tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_CALL_LOG = Path({str(call_log)!r})

_INTERNAL_AGENTS = [
    ("archivist-1",            "Archivist",            "archivist",            "{{}}"),
    ("data-steward-1",         "Data Steward",         "data-steward",         "{{}}"),
    ("dba-1",                  "DBA",                  "dba",                  "{{}}"),
    ("librarian-1",            "Librarian",            "librarian",            "{{}}"),
    ("reference-librarian-1",  "Reference Librarian",  "reference-librarian",  "{{}}"),
]


class InternalAgentsMissingError(RuntimeError):
    pass


def ensure_internal_agents(db_path: str) -> dict:
    # Record the call so the test can assert the wiring fired.
    with _CALL_LOG.open("a", encoding="utf-8") as f:
        f.write(str(db_path) + "\\n")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        present = {{r["id"] for r in conn.execute("SELECT id FROM agents")}}
        missing_before = [aid for aid, *_ in _INTERNAL_AGENTS if aid not in present]

        if not missing_before:
            return {{
                "status": "already_present",
                "missing_before": [],
                "present_after": [aid for aid, *_ in _INTERNAL_AGENTS],
            }}

        # Seed the 5 internal roles + agents. agents.role_id is a
        # FK to roles.id; insert role row first if absent.
        for agent_id, name, role_name, profile in _INTERNAL_AGENTS:
            row = conn.execute(
                "SELECT id FROM roles WHERE name = ?", (role_name,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO roles (name, description) VALUES (?, ?)",
                    (role_name, f"internal-memex: {{role_name}}"),
                )
                role_id = cur.lastrowid
            else:
                role_id = row["id"]
            if agent_id in present:
                continue
            conn.execute(
                "INSERT INTO agents (id, name, role_id, profile) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, name, role_id, profile),
            )
        conn.commit()
        return {{
            "status": "repaired",
            "missing_before": missing_before,
            "present_after": [aid for aid, *_ in _INTERNAL_AGENTS],
        }}
    finally:
        conn.close()
'''
    (plugin_root / "scripts" / "install.py").write_text(shim, encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home + $HOME + $MEMEX_HOME to a tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MEMEX_HOME", str(home / ".memex"))
    return {"home": home}


@pytest.fixture
def memex_install_with_shim(tmp_path, fake_home):
    """Stand up a hermetic memex install AND plant the
    `ensure_internal_agents` shim under the plugin's scripts dir.

    Returns paths the test will assert on.
    """
    plugin_root = tmp_path / "memex_plugin"
    _copy_memex_min_into(plugin_root)
    call_log = tmp_path / "ensure_internal_agents.log"
    _write_ensure_internal_agents_shim(plugin_root, call_log=call_log)

    memex_home = fake_home["home"] / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))

    agents_db = memex_home / "agents.db"
    schema_sql = (plugin_root / "db" / "agents.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(str(agents_db))
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

    # NOTE: deliberately do NOT seed the 5 internal Memex agents into
    # agents.db. That simulates the live-machine bug from issue #6 bug
    # #3 — agents.db exists with atelier's roster about to be added,
    # but memex's own invariants are missing.

    from scripts import backend_memex, mode_detector

    mode_detector._clear_cache()
    backend_memex._load_memex_module.cache_clear()

    return {
        "home": fake_home["home"],
        "memex_home": memex_home,
        "plugin_root": plugin_root,
        "agents_db": str(agents_db),
        "call_log": call_log,
    }


@pytest.fixture
def memex_install_without_install_module(tmp_path, fake_home):
    """Same as memex_install_with_shim, but WITHOUT planting a
    scripts/install.py. Used to exercise the soft-import fallback
    (memex < 2.6.0): bootstrap must not crash.
    """
    plugin_root = tmp_path / "memex_plugin"
    _copy_memex_min_into(plugin_root, version="2.5.1")
    # No scripts/install.py planted — the soft import will raise
    # ImportError and atelier should swallow it.

    memex_home = fake_home["home"] / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))

    agents_db = memex_home / "agents.db"
    schema_sql = (plugin_root / "db" / "agents.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(str(agents_db))
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

    from scripts import backend_memex, mode_detector

    mode_detector._clear_cache()
    backend_memex._load_memex_module.cache_clear()

    return {
        "memex_home": memex_home,
        "plugin_root": plugin_root,
        "agents_db": str(agents_db),
    }


# ── Regression body ───────────────────────────────────────────────────────────


def test_bootstrap_calls_ensure_internal_agents(memex_install_with_shim):
    """After atelier's bootstrap seeds its 61-role roster into
    ~/.memex/agents.db, it MUST call memex's ensure_internal_agents()
    so the 5 internal Memex agents are restored.

    Asserts:
      1. The shim's `ensure_internal_agents` was invoked exactly once
         with the path to ~/.memex/agents.db.
      2. All 5 internal Memex agents are present in agents.db after
         bootstrap (the shim's job — proves atelier really invoked it
         and the side effect landed).
    """
    from scripts.bootstrap import run_bootstrap

    result = run_bootstrap()
    assert result["mode"] == "memex"

    # (1) The wiring fired exactly once, with the right path.
    call_log = memex_install_with_shim["call_log"]
    assert call_log.exists(), (
        "ensure_internal_agents was never invoked — atelier bootstrap "
        "did not wire the memex post-touch invariant restore."
    )
    calls = call_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(calls) == 1, f"expected exactly one call, got {len(calls)}: {calls}"
    assert calls[0] == memex_install_with_shim["agents_db"]

    # (2) The 5 internal Memex agents are now in agents.db.
    conn = sqlite3.connect(memex_install_with_shim["agents_db"])
    try:
        present = {r[0] for r in conn.execute("SELECT id FROM agents")}
    finally:
        conn.close()
    missing = [aid for aid in INTERNAL_MEMEX_AGENT_IDS if aid not in present]
    assert not missing, (
        f"after bootstrap, {len(missing)} internal Memex agent(s) "
        f"still missing from agents.db: {missing}"
    )


def test_bootstrap_soft_import_missing_install_module(memex_install_without_install_module):
    """Soft-import backward-compat: when memex < 2.6.0 (no
    `scripts.install.ensure_internal_agents`), atelier bootstrap MUST
    NOT crash. It logs / silently continues — atelier's seeding still
    completes and the marker still lands.
    """
    from scripts.bootstrap import run_bootstrap

    # Should complete without raising even though no scripts/install.py
    # exists in the plugin tree.
    result = run_bootstrap()
    assert result["mode"] == "memex"

    # Atelier's own seeding still landed.
    conn = sqlite3.connect(memex_install_without_install_module["agents_db"])
    try:
        role_count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    finally:
        conn.close()
    assert role_count > 0
    assert agent_count > 0
