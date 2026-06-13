# tests/test_regression_backend_memex_v210_write_path_imports.py
"""Regression tests for the Memex >= 2.10 write-path import crashes in
`scripts/backend_memex.py` (every Memex-mode `_atelier_write` — project
create, session write, workflow advance — dies under memex/2.10+).

Two distinct mechanisms, both rooted in the Atelier/Memex `scripts`
namespace collision:

1. EXEC-time shim under-coverage. `_memex_validate_output` loads
   `agents.librarian` via `_load_memex_module`, whose exec is wrapped in
   `_scripts_db_shim` — but the shim only injects Memex's `db` / `paths` /
   `registry` as `sys.modules['scripts.*']`. Memex >= 2.10
   `agents/librarian.py` ALSO does module-level

       from scripts import agents as agents_mod
       from scripts import stores

   which resolve against ATELIER's own `scripts` package:

       ImportError: cannot import name 'stores' from 'scripts'

   Worse, `from scripts import agents` does NOT even fail — it silently
   binds Atelier's real `scripts/agents.py` (the package attribute wins
   over any `sys.modules` injection), so even a "fixed" sys.modules-only
   shim would hand Memex code the wrong module.

2. DEFERRED call-time imports. Memex code performs `from scripts.X
   import ...` INSIDE function bodies, long after the exec-scoped shim
   has exited. Observed trigger: `_try_embed` -> `EmbeddingUnavailable`
   -> `_memex_log_embedding_skip` -> `embeddings.log_skip` ->
   `_append_skip_log` -> `from scripts.db import memex_home`:

       ModuleNotFoundError: No module named 'scripts.db'

   Same class of bug on the embed SUCCESS path (`embeddings.encode` ->
   `_record_model_info` -> `from scripts import registry`) and on
   `db.require_bootstrap`'s error path (`from scripts.paths import
   PLUGIN_ROOT`), which every `stores` CRUD helper calls.

The fake plugin tree below mirrors memex/2.11.0's import shapes exactly
(file-by-file comments cite the real source lines). Hermetic: no real
Memex install, no ~/.memex — `_memex_plugin_root` is monkeypatched and
the fake `db.memex_home()` honors a test-controlled env var.
"""

from __future__ import annotations

import json as _json
import sys

import pytest

from scripts import backend_memex


def _build_fake_memex_210_plugin(plugin_root) -> None:
    """Materialize a synthetic Memex >= 2.10 plugin tree under
    `plugin_root`, mirroring the import shapes of memex/2.11.0:

      - `agents/librarian.py` does `from scripts import agents as
        agents_mod` and `from scripts import stores` at MODULE level
        (memex/2.11.0/scripts/agents/librarian.py:42-43).
      - `agents/reference_librarian.py` does `from scripts import
        embeddings` at MODULE level (reference_librarian.py:38).
      - `embeddings.py` defers `from scripts.db import memex_home` to
        CALL time inside `_append_skip_log` (embeddings.py:82) and
        `from scripts import registry` inside the encode success path
        (mirrors `_record_model_info`, embeddings.py:326).
      - `db.require_bootstrap` defers `from scripts.paths import
        PLUGIN_ROOT` to its error path (db.py:104); every `stores` CRUD
        helper calls `require_bootstrap()` first (stores.py:84+).

    Every module is stdlib-only so the test never depends on a real
    Memex install.
    """
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "memex", "version": "2.10.99-test"})
    )
    scripts_dir = plugin_root / "scripts"
    agents_dir = scripts_dir / "agents"
    agents_dir.mkdir(parents=True)
    (scripts_dir / "__init__.py").write_text("")

    # db.py — memex_home honors FAKE_MEMEX_HOME so the test controls
    # where the skip log lands. require_bootstrap mirrors the deferred
    # `from scripts.paths import PLUGIN_ROOT` of memex/2.11.0 db.py:104.
    (scripts_dir / "db.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "MEMEX_DB_MARKER = 'memex-db'\n"
        "def memex_home():\n"
        "    return Path(os.environ['FAKE_MEMEX_HOME'])\n"
        "def get_connection(db_path):\n"
        "    return ('fake-conn', db_path)\n"
        "def require_bootstrap():\n"
        "    if not (memex_home() / 'registry.json').exists():\n"
        "        from scripts.paths import PLUGIN_ROOT  # deferred, db.py:104\n"
        "        raise RuntimeError(f'Memex is not bootstrapped (plugin at {PLUGIN_ROOT})')\n"
    )

    # paths.py — stdlib-only, same names memex's real paths.py exports.
    (scripts_dir / "paths.py").write_text(
        "from pathlib import Path\n"
        "PLUGIN_ROOT = Path(__file__).resolve().parent.parent\n"
        "DB_DIR = PLUGIN_ROOT / 'db'\n"
        "PROMPTS_DIR = PLUGIN_ROOT / 'prompts'\n"
    )

    # registry.py — top-level `from scripts.db import memex_home`
    # (memex/2.11.0 registry.py:13).
    (scripts_dir / "registry.py").write_text(
        "from scripts.db import MEMEX_DB_MARKER\n"
        "BOUND_MARKER = MEMEX_DB_MARKER\n"
        "REGISTRY_MARKER = 'memex-registry'\n"
    )

    # stores.py — exact import shape of memex/2.11.0 stores.py:8-10;
    # every CRUD helper calls require_bootstrap() first (stores.py:84+).
    (scripts_dir / "stores.py").write_text(
        "from scripts import registry\n"
        "from scripts.db import get_connection, require_bootstrap\n"
        "from scripts.paths import DB_DIR\n"
        "STORES_MARKER = 'memex-stores'\n"
        "BOUND_REGISTRY = registry\n"
        "def safe_identifier(name):\n"
        "    return name\n"
        "def query(name, sql, params=()):\n"
        "    require_bootstrap()\n"
        "    return []\n"
        "def insert(name, table, row):\n"
        "    require_bootstrap()\n"
        "    return dict(row)\n"
        "def update(name, table, row_id, updates):\n"
        "    require_bootstrap()\n"
        "    return dict(updates)\n"
        "def delete(name, table, row_id):\n"
        "    require_bootstrap()\n"
        "    return True\n"
    )

    # agents_shadow_probe.py — minimal victim for the silent-shadow half
    # of the bug: it imports ONLY `scripts.agents`, so (unlike
    # librarian.py above) nothing else can crash first and mask a wrong
    # binding. See test_regression_scripts_agents_shadow_defeated_by_probe.
    (scripts_dir / "agents_shadow_probe.py").write_text(
        "from scripts import agents as agents_mod\nPROBE_MARKER = 'shadow-probe'\n"
    )

    # agents/__init__.py — top-level `from scripts.db import
    # get_connection` (memex/2.11.0 agents/__init__.py:19).
    (agents_dir / "__init__.py").write_text(
        "from scripts.db import get_connection\nAGENTS_MARKER = 'memex-agents'\n"
    )

    # agents/librarian.py — THE memex >= 2.10 shape this regression is
    # about (librarian.py:42-45).
    (agents_dir / "librarian.py").write_text(
        "from scripts import agents as agents_mod\n"
        "from scripts import stores\n"
        "from scripts.db import get_connection, memex_home\n"
        "from scripts.paths import PROMPTS_DIR\n"
        "LIBRARIAN_MARKER = 'memex-librarian'\n"
        "class DuplicateKeyError(Exception):\n"
        "    pass\n"
        "def validate_output(obj):\n"
        "    return dict(obj)\n"
    )

    # agents/reference_librarian.py — module-level `from scripts import
    # embeddings` / `agents` (reference_librarian.py:37-40).
    (agents_dir / "reference_librarian.py").write_text(
        "from scripts import agents as agents_mod\n"
        "from scripts import embeddings\n"
        "from scripts.db import get_connection, memex_home\n"
        "from scripts.paths import PROMPTS_DIR\n"
        "REFLIB_MARKER = 'memex-reference-librarian'\n"
        "def execute_query_plan(plan, with_embedding=True):\n"
        "    return []\n"
    )

    # embeddings.py — the deferred call-time imports. `encode` mirrors
    # the real encode's success path (`_record_model_info` does
    # `from scripts import registry`, embeddings.py:326) and raises
    # EmbeddingUnavailable when FAKE_EMBED_FAIL is set so tests can
    # drive the `_try_embed` -> log_skip trigger path. `_append_skip_log`
    # mirrors embeddings.py:82.
    (scripts_dir / "embeddings.py").write_text(
        "import os\n"
        "from scripts.db import require_bootstrap\n"
        "class EmbeddingUnavailable(Exception):\n"
        "    def __init__(self, reason, provider, detail=''):\n"
        "        self.reason = reason\n"
        "        self.provider = provider\n"
        "        self.detail = detail\n"
        "        super().__init__(f'embedding unavailable ({provider}/{reason})')\n"
        "def _append_skip_log(entry):\n"
        "    from scripts.db import memex_home  # deferred, embeddings.py:82\n"
        "    audits_dir = memex_home() / 'audits'\n"
        "    audits_dir.mkdir(parents=True, exist_ok=True)\n"
        "    with open(audits_dir / 'embedding-skip-log.md', 'a', encoding='utf-8') as f:\n"
        "        f.write(entry)\n"
        "def log_skip(exc, *, caller_agent_id='', index_id='', input_chars=0):\n"
        "    _append_skip_log(f'{caller_agent_id} {index_id} {input_chars}\\n')\n"
        "def encode(text):\n"
        "    if os.environ.get('FAKE_EMBED_FAIL'):\n"
        "        raise EmbeddingUnavailable('no-api-key', 'openai')\n"
        "    from scripts import registry  # deferred, embeddings.py:326\n"
        "    assert registry.REGISTRY_MARKER == 'memex-registry'\n"
        "    return b'\\x01\\x02'\n"
    )


@pytest.fixture
def fake_memex_210(tmp_path, monkeypatch):
    """Fake memex >= 2.10 plugin tree + a writable fake ~/.memex home.

    Pins `_memex_plugin_root` to the fake tree and busts the
    `_load_memex_module` cache on both sides so neighboring tests are
    isolated (same convention as the shim-recursion regression test).
    """
    plugin = tmp_path / "memex_plugin"
    _build_fake_memex_210_plugin(plugin)
    home = tmp_path / "memex_home"
    home.mkdir()
    (home / "registry.json").write_text("{}")  # bootstrapped by default
    monkeypatch.setenv("FAKE_MEMEX_HOME", str(home))
    monkeypatch.delenv("FAKE_EMBED_FAIL", raising=False)
    monkeypatch.setattr(backend_memex, "_memex_plugin_root", lambda: plugin)
    backend_memex._load_memex_module.cache_clear()
    yield {"plugin": plugin, "home": home}
    backend_memex._load_memex_module.cache_clear()


# ── Mechanism 1: exec-time shim under-coverage ─────────────────────────────


def test_regression_librarian_exec_resolves_stores_and_agents_against_memex(fake_memex_210):
    """Loading memex >= 2.10 `agents.librarian` must succeed AND bind its
    module-level `stores` / `agents_mod` names to MEMEX's modules.

    On un-patched code this raises
    `ImportError: cannot import name 'stores' from 'scripts'` because the
    exec-scope shim only injects db/paths/registry.
    """
    # Plant the hazard: Atelier's REAL scripts/agents.py imported first,
    # so the `scripts` package carries an `agents` attribute that wins
    # over sys.modules injection in `from scripts import agents`.
    import scripts.agents as atelier_agents

    try:
        librarian = backend_memex._memex_module("agents.librarian")
    except ImportError as exc:  # pragma: no cover -- the bug path
        pytest.fail(
            "memex>=2.10 agents/librarian.py failed to exec under the "
            f"scripts shim (shim coverage missing stores/agents): {exc}"
        )

    assert librarian.LIBRARIAN_MARKER == "memex-librarian"
    assert librarian.stores.STORES_MARKER == "memex-stores"
    # The silent-shadow half of the bug: agents_mod must be MEMEX's
    # agents package, not Atelier's scripts/agents.py.
    assert getattr(librarian.agents_mod, "AGENTS_MARKER", None) == "memex-agents", (
        "librarian's `from scripts import agents as agents_mod` bound "
        f"{librarian.agents_mod!r} — Atelier's scripts/agents.py shadowed "
        "Memex's agents package during the librarian exec"
    )

    # And the shim must have RESTORED Atelier's module afterwards: both
    # the sys.modules entry and the package attribute.
    assert sys.modules["scripts.agents"] is atelier_agents
    import scripts as scripts_pkg

    assert scripts_pkg.agents is atelier_agents
    from scripts import agents as post_shim_agents

    assert post_shim_agents is atelier_agents


def test_regression_reference_librarian_exec_resolves_embeddings(fake_memex_210):
    """memex >= 2.10 `agents/reference_librarian.py` does module-level
    `from scripts import embeddings` — the shim must cover it too
    (read path: `_memex_search` -> `find_documents`)."""
    reflib = backend_memex._memex_module("agents.reference_librarian")
    assert reflib.REFLIB_MARKER == "memex-reference-librarian"
    assert hasattr(reflib.embeddings, "encode")
    assert getattr(reflib.agents_mod, "AGENTS_MARKER", None) == "memex-agents"


# ── Mechanism 2: deferred call-time imports ────────────────────────────────


def test_regression_log_skip_deferred_import_resolves_at_call_time(fake_memex_210):
    """`embeddings.log_skip` -> `_append_skip_log` does
    `from scripts.db import memex_home` at CALL time, after the
    exec-scope shim has exited. On un-patched code:

        ModuleNotFoundError: No module named 'scripts.db'
    """
    embeddings = backend_memex._memex_module("embeddings")
    exc = embeddings.EmbeddingUnavailable("no-api-key", "openai")
    try:
        backend_memex._memex_log_embedding_skip(
            exc, caller_agent_id="dr-okafor", index_id="idx-1", input_chars=42
        )
    except ModuleNotFoundError as e:  # pragma: no cover -- the bug path
        pytest.fail(
            "deferred `from scripts.db import memex_home` inside "
            f"embeddings._append_skip_log did not resolve at call time: {e}"
        )
    log = fake_memex_210["home"] / "audits" / "embedding-skip-log.md"
    assert log.exists()
    assert "dr-okafor idx-1 42" in log.read_text()


def test_regression_embed_success_path_deferred_registry_import(fake_memex_210):
    """`embeddings.encode` records model info on SUCCESS via a deferred
    `from scripts import registry` (memex/2.11.0 embeddings.py:326) —
    `_memex_embed` must run it with the shim active."""
    assert backend_memex._memex_embed("hello world") == b"\x01\x02"


def test_regression_try_embed_skip_trigger_path_end_to_end(fake_memex_210, monkeypatch):
    """The production trigger: `_try_embed` -> encode raises
    EmbeddingUnavailable -> `_memex_log_embedding_skip` -> deferred
    `from scripts.db import memex_home`. Must return None (FTS5-only
    write proceeds) and append the audit row."""
    monkeypatch.setenv("FAKE_EMBED_FAIL", "1")
    result = backend_memex._try_embed(
        "some searchable text", caller_agent_id="dr-okafor", index_id="idx-2"
    )
    assert result is None
    log = fake_memex_210["home"] / "audits" / "embedding-skip-log.md"
    assert log.exists()
    assert "dr-okafor idx-2 20" in log.read_text()


def test_regression_core_query_bootstrap_error_path_deferred_paths_import(fake_memex_210):
    """`stores.query` calls `db.require_bootstrap()`, whose not-bootstrapped
    branch defers `from scripts.paths import PLUGIN_ROOT` (db.py:104).
    With the registry file removed, the operator must see the clean
    bootstrap RuntimeError — not a ModuleNotFoundError from the deferred
    import resolving against Atelier's scripts package."""
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        backend_memex._memex_core_query(store="atelier", table="tasks")


def test_regression_scripts_agents_shadow_defeated_by_probe(fake_memex_210):
    """Dedicated probe for the silent-shadow half of mechanism 1, isolated
    from the loud ImportError half.

    The librarian-shape test above dies on `from scripts import stores`
    BEFORE its agents_mod assertion can run, so pre-fix it never proves
    the shadow. A hypothetical sys.modules-only shim (injecting
    `sys.modules['scripts.agents']` without overriding the `scripts`
    package ATTRIBUTE) would pass that test's import yet still hand Memex
    code Atelier's real `scripts/agents.py` — `from scripts import
    agents` consults `getattr(scripts, 'agents')` first, and the
    attribute exists once Atelier's module has been imported anywhere in
    the process.

    This probe module imports ONLY `scripts.agents`, so the binding
    identity is the one and only thing under test: on a sys.modules-only
    shim the first assertion goes red (agents_mod IS Atelier's module);
    on the attribute-overriding shim it binds Memex's agents package.
    """
    import scripts as scripts_pkg
    import scripts.agents as atelier_agents  # plant the package attribute

    assert scripts_pkg.agents is atelier_agents  # precondition: shadow armed

    probe = backend_memex._memex_module("agents_shadow_probe")
    assert probe.agents_mod is not atelier_agents, (
        "probe's `from scripts import agents` bound ATELIER's "
        "scripts/agents.py — the shim exposed sys.modules but not the "
        "`scripts` package attribute, so the existing attribute won"
    )
    assert getattr(probe.agents_mod, "AGENTS_MARKER", None) == "memex-agents"

    # And the shadow is restored bit-for-bit outside the shim scope.
    assert sys.modules["scripts.agents"] is atelier_agents
    assert scripts_pkg.agents is atelier_agents


# ── Boundary: Atelier modules OUTSIDE backend_memex.py ─────────────────────
#
# roles.py / workflow.py / session.py reach Memex stores through
# backend_memex helpers. They must get the same call-shim coverage as
# backend_memex's own wrappers — the canonical symptom is again
# `db.require_bootstrap`'s deferred `from scripts.paths import
# PLUGIN_ROOT` (db.py:104): un-shimmed, the operator sees a raw
# ModuleNotFoundError instead of Memex's bootstrap guidance.


def test_regression_roles_list_roles_memex_mode_bootstrap_error_is_clean(
    fake_memex_210, monkeypatch
):
    """`roles.list_roles` (memex mode, raw ORDER BY SQL) must surface the
    clean bootstrap RuntimeError, not ModuleNotFoundError."""
    from scripts import mode_detector, roles

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        roles.list_roles("ignored.db")


def test_regression_roles_update_role_memex_mode_bootstrap_error_is_clean(
    fake_memex_210, monkeypatch
):
    """`roles.update_role` (memex mode, stores.update) — same contract."""
    from scripts import mode_detector, roles

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        roles.update_role("ignored.db", 1, description="new description")


def test_regression_workflow_catalog_query_memex_mode_bootstrap_error_is_clean(
    fake_memex_210, monkeypatch
):
    """`workflow._catalog_query` (memex mode, via get_phase) — same
    contract."""
    from scripts import mode_detector, workflow

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        workflow.get_phase("ignored.db", 1)


def test_regression_agents_search_memex_mode_bootstrap_error_is_clean(fake_memex_210, monkeypatch):
    """`agents.search_agents` (memex mode, raw LIKE SQL) — same contract.
    Found in self-review beyond the reviewer's six sites: scripts/agents.py
    reached `stores.query` / `stores.delete` directly too."""
    from scripts import agents as agents_module
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        agents_module.search_agents("ignored.db", "okafor")


def test_regression_agents_delete_fallback_memex_mode_bootstrap_error_is_clean(
    fake_memex_210, monkeypatch
):
    """`agents.delete_agent`'s older-Memex stores fallback — same contract.
    The fake agents package (like pre-2.x Memex) has no `delete_agent`
    helper, so the fallback branch is the one exercised."""
    from scripts import agents as agents_module
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        agents_module.delete_agent("ignored.db", "agent-okafor")


def test_regression_meetings_search_memex_mode_routes_through_facade(fake_memex_210, monkeypatch):
    """`meetings.search_meetings` (memex mode, raw LIKE SQL). Found in
    self-review: it used the legacy `_ensure_memex_importable()` +
    `from scripts import stores` pattern, which `backend_memex` itself
    documents as broken-by-design (Atelier's `scripts` package wins the
    submodule resolution → ImportError before any query runs). Must
    route through the backend_memex facade: clean bootstrap RuntimeError
    when not bootstrapped, never ImportError/ModuleNotFoundError."""
    from scripts import meetings, mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        meetings.search_meetings("ignored.db", query="roadmap")


def test_regression_meetings_get_participants_memex_mode_routes_through_facade(
    fake_memex_210, monkeypatch
):
    """`meetings.get_participants` (memex mode, JOIN SQL) — same legacy
    broken-import pattern, same facade contract."""
    from scripts import meetings, mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        meetings.get_participants("ignored.db", meeting_id=1)


def test_regression_session_prune_memex_mode_bootstrap_error_is_clean(fake_memex_210, monkeypatch):
    """`session.prune_sessions`'s memex-mode DELETE loop — same contract.
    `_memex_core_query` is stubbed so the test reaches the delete call
    site (the un-shimmed boundary under test) rather than failing in the
    already-covered read helper."""
    import scripts.session as session_module
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(
        backend_memex,
        "_memex_core_query",
        lambda *, store, table, where: [{"id": 1, "project_id": 7}, {"id": 2, "project_id": 7}],
    )
    (fake_memex_210["home"] / "registry.json").unlink()
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        session_module.prune_sessions("ignored.db", project_id=7, keep=1)
