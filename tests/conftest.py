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
