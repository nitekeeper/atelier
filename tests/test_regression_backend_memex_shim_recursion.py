# tests/test_regression_backend_memex_shim_recursion.py
"""Regression test for an infinite recursion between `_scripts_db_shim`
and `_load_memex_module` in `scripts/backend_memex.py`.

Symptom (observed against the real Memex v2.5.1 plugin):

    PYTHONPATH=. python3 scripts/projects.py list
    ...
    File ".../scripts/backend_memex.py", line 910, in _memex_core_query
        memex_stores = _memex_module("stores")
    File ".../scripts/backend_memex.py", line 239, in _memex_module
        return _load_memex_module(_memex_plugin_root(), dotted)
    File ".../scripts/backend_memex.py", line 229, in _load_memex_module
        with _scripts_db_shim(plugin_root):
    File ".../scripts/backend_memex.py", line 151, in _scripts_db_shim
        memex_paths = _load_memex_module(plugin_root, "paths")
    File ".../scripts/backend_memex.py", line 229, in _load_memex_module
        with _scripts_db_shim(plugin_root):
    File ".../scripts/backend_memex.py", line 151, in _scripts_db_shim
        memex_paths = _load_memex_module(plugin_root, "paths")
    ... (unbounded; RecursionError)

Mechanism:
  1. `_load_memex_module(root, "stores")` is called.
  2. Because "stores" != "db", it enters `_scripts_db_shim(root)` around
     `exec_module`.
  3. The shim calls `_load_memex_module(root, "paths")`. The
     `@functools.cache` decorator does NOT memoize a result that is still
     being computed, so the call re-enters `_load_memex_module` for "paths".
  4. "paths" != "db", so it enters `_scripts_db_shim(root)` again.
  5. The new shim once again calls `_load_memex_module(root, "paths")`.
     Same args, no cached result yet — infinite recursion.

This test stands up a minimal Memex-shaped plugin tree (db.py, paths.py,
registry.py, plus a victim module that imports nothing exotic) and calls
`_memex_module("stores_like")`. On current code the call raises
`RecursionError`. After the fix it must return the loaded module.

Hermetic: no real Memex plugin is touched; `_memex_plugin_root` is
monkey-patched at the `backend_memex` module level.
"""

from __future__ import annotations

import json as _json
import sys

import pytest

from scripts import backend_memex


def _build_fake_memex_plugin(plugin_root):
    """Materialize a synthetic Memex plugin tree under `plugin_root`
    containing the three modules the shim eagerly loads (`db`, `paths`,
    `registry`) plus a victim `stores_like` module that does the same
    `from scripts.* import …` dance Memex's real stores.py does.

    Every module is stdlib-only so the test never depends on a real
    Memex install, and every module is shaped exactly the way Memex's
    own scripts are shaped (`from scripts.db import ...`,
    `from scripts.paths import ...`, `from scripts import registry`).
    """
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "memex", "version": "test"})
    )
    scripts_dir = plugin_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "__init__.py").write_text("")

    # 1. db.py — stdlib-only, mimics Memex's real db.py shape. Exposes a
    # sentinel (MEMEX_DB_MARKER) so the registry-injection assertion
    # below can prove the snapshot/restore plumbing in
    # `_load_memex_module`'s registry branch actually exposes THIS db
    # module under `sys.modules['scripts.db']` for registry's exec.
    (scripts_dir / "db.py").write_text(
        "MEMEX_DB_MARKER = 'memex-db'\n"
        "def get_connection(db_path):\n"
        "    return ('fake-conn', db_path)\n"
    )

    # 2. paths.py — stdlib-only, defines DB_DIR (one of the names Memex's
    # `stores.py` imports). Crucially this module does NOT import from
    # `scripts.*` at top level; the recursion still triggers because the
    # shim itself unconditionally re-enters `_load_memex_module("paths")`.
    (scripts_dir / "paths.py").write_text(
        "from pathlib import Path\n"
        "DB_DIR = Path(__file__).resolve().parent.parent / 'db'\n"
    )

    # 3. registry.py — exercises the registry-injection bootstrap branch.
    # Memex's real registry.py does `from scripts.db import memex_home`
    # at top level (verified against memex/2.5.1/scripts/registry.py:13).
    # Our fake registry mirrors that pattern by binding MEMEX_DB_MARKER
    # so the test can assert the right `scripts.db` was visible during
    # the registry exec.
    (scripts_dir / "registry.py").write_text(
        "from scripts.db import MEMEX_DB_MARKER\n"
        "BOUND_MARKER = MEMEX_DB_MARKER\n"
        "REGISTRY_MARKER = 'memex-registry'\n"
    )

    # 4. The victim module — mirrors the kind of intra-package imports
    # real Memex modules do, so that even after the shim is fixed the
    # module exec succeeds end-to-end.
    (scripts_dir / "stores_like.py").write_text(
        "from scripts.db import get_connection\n"
        "from scripts.paths import DB_DIR\n"
        "from scripts import registry\n"
        "MARKER = 'stores-like'\n"
        "BOUND_DB = get_connection\n"
        "BOUND_DB_DIR = DB_DIR\n"
        "BOUND_REGISTRY = registry\n"
    )

    # 5. A second victim — used to verify the cache-warm second-load path
    # (no leakage of `scripts.*` state across consecutive loads).
    (scripts_dir / "stores_like_2.py").write_text(
        "from scripts.db import get_connection\n"
        "from scripts.paths import DB_DIR\n"
        "from scripts import registry\n"
        "MARKER = 'stores-like-2'\n"
        "BOUND_DB = get_connection\n"
        "BOUND_DB_DIR = DB_DIR\n"
        "BOUND_REGISTRY = registry\n"
    )


def _snapshot_scripts_keys() -> dict:
    """Snapshot the (presence, value) of every `scripts.*` key the shim
    might touch. Used to assert the load is a no-op on sys.modules."""
    return {
        key: (key in sys.modules, sys.modules.get(key))
        for key in ("scripts.db", "scripts.paths", "scripts.registry")
    }


def _assert_scripts_keys_restored(pre: dict) -> None:
    for key, (was_present, prior) in pre.items():
        post_present = key in sys.modules
        post_value = sys.modules.get(key)
        assert post_present is was_present, (
            f"shim leaked: {key!r} presence changed across the load "
            f"(pre={was_present}, post={post_present})"
        )
        assert post_value is prior, (
            f"shim leaked: {key!r} identity changed across the load"
        )


def test_scripts_db_shim_does_not_recurse_on_paths_load(tmp_path, monkeypatch):
    """`_load_memex_module(root, "stores_like")` must NOT raise
    `RecursionError`.

    On current code the inner `_scripts_db_shim` re-enters itself via
    `_load_memex_module(root, "paths")` because `functools.cache` has no
    entry yet for the still-executing call. The recursion is unbounded.

    After the fix the call returns the loaded module with its
    intra-package imports correctly resolved against the shim.

    This test additionally exercises:
      - The registry-injection bootstrap branch (`registry.py` does
        `from scripts.db import MEMEX_DB_MARKER` — only succeeds if the
        bootstrap branch temporarily injects Memex's `db` into
        `sys.modules['scripts.db']` during registry exec).
      - The snapshot/restore semantics: `sys.modules['scripts.*']` must
        be bit-for-bit identical before and after the load.
      - A second-load cache-warm path: a second victim module loads
        cleanly with no state leakage.
    """
    plugin = tmp_path / "memex_plugin"
    _build_fake_memex_plugin(plugin)

    monkeypatch.setattr(backend_memex, "_memex_plugin_root", lambda: plugin)
    # Bust the lru_cache so this test's fake-plugin path doesn't pollute
    # adjacent tests (and isn't itself polluted by them).
    backend_memex._load_memex_module.cache_clear()

    # Snapshot pre-state so we can verify the shim restores sys.modules
    # cleanly even on the success path.
    pre_present = _snapshot_scripts_keys()

    # Bound the recursion-depth budget so a regressed implementation
    # fails fast (sub-second) instead of producing a 10 000-line trace.
    prior_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        try:
            mod = backend_memex._memex_module("stores_like")
        except RecursionError as exc:  # pragma: no cover -- the bug path
            pytest.fail(
                "RecursionError raised by _load_memex_module / "
                f"_scripts_db_shim — the shim recurses into itself: {exc}"
            )

        # MAJOR #1 — prove the registry-injection branch actually ran.
        # `registry.py`'s top-level `from scripts.db import MEMEX_DB_MARKER`
        # only succeeds if the bootstrap branch put Memex's db module
        # under `sys.modules['scripts.db']` during registry exec.
        registry_mod = backend_memex._load_memex_module(plugin, "registry")
        assert registry_mod.BOUND_MARKER == "memex-db", (
            "registry-injection branch did not expose Memex's `db` "
            "module under `sys.modules['scripts.db']` during the "
            "registry exec — `from scripts.db import MEMEX_DB_MARKER` "
            "would have failed or bound the wrong value."
        )

        # Assert sys.modules cleanup AFTER registry load too.
        _assert_scripts_keys_restored(pre_present)

        # MAJOR #2 — second-load (cache-warm) path. Load a second victim
        # module via `_load_memex_module` directly (avoids the
        # plugin_root resolution baked into `_memex_module`) and confirm
        # no `scripts.*` state has leaked.
        mod2 = backend_memex._load_memex_module(plugin, "stores_like_2")
        assert mod2.MARKER == "stores-like-2"
        assert mod2.BOUND_DB("y.db") == ("fake-conn", "y.db")
        _assert_scripts_keys_restored(pre_present)
    finally:
        sys.setrecursionlimit(prior_limit)
        backend_memex._load_memex_module.cache_clear()

    # Success-path assertions: the module loaded, the shim restored
    # `sys.modules['scripts.*']` to its pre-state, and the victim
    # module's captured references point at the Memex helpers.
    assert mod.MARKER == "stores-like"
    assert mod.BOUND_DB("x.db") == ("fake-conn", "x.db")

    _assert_scripts_keys_restored(pre_present)
