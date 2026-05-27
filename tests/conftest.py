# tests/conftest.py
"""
Conftest: mock preflight.check at collection time so importing workspace.py
during test collection does not trigger real platform detection.
"""

from unittest.mock import patch

import pytest

# Started before any test file is imported. Ensures workspace.py can be
# imported safely in all environments (CI, Docker, no-tmux machines).
_preflight_check_patcher = patch("scripts.preflight.check")
_preflight_check_patcher.start()


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    """Reset `detect_mode` cache before AND after each test.

    Promoted from `test_backend_dispatch.py` so the dispatch + skeleton
    suites (and any future facade test) share a single hermetic-mode
    guarantee — a test that monkey-patches `detect_mode` cannot leak
    into the next file.
    """
    from scripts import mode_detector

    mode_detector._clear_cache()
    yield
    mode_detector._clear_cache()


@pytest.fixture(autouse=True)
def _stub_singleton_workspace(monkeypatch):
    """Stub `backend_memex._singleton_workspace` and
    `_workspace_slug_for_id` to the legacy `"atelier"` slug.

    atelier#55 removed the `_WORKSPACE_SLUG = "atelier"` constant in
    favor of looking up the singleton workspace (or the
    workspace_id-specific slug) via `_memex_core_query`. The pre-
    existing `_atelier_write`-driver tests in
    `test_backend_memex_documents.py` and the document/meeting Memex-
    mode regression tests stub `_memex_write_entry` etc. but don't
    stand up a real Memex registry, so the new lookups would crash
    with `MemexNotInitializedError` or `ValueError`.

    This autouse fixture short-circuits BOTH lookups with the legacy
    `"atelier"` slug — restoring pre-#55 test behavior for tests that
    don't need real workspace resolution. Tests that DO need to
    exercise the real lookup (e.g. integration tests that hit a real
    Memex bootstrap) can override with their own `monkeypatch.setattr`
    AFTER this fixture runs (pytest applies the override at the test-
    function scope, overriding the conftest-level patch).
    """
    from scripts import backend_memex

    monkeypatch.setattr(
        backend_memex,
        "_singleton_workspace",
        lambda: {"id": 1, "slug": "atelier"},
    )
    monkeypatch.setattr(
        backend_memex,
        "_workspace_slug_for_id",
        lambda workspace_id: "atelier",
    )
