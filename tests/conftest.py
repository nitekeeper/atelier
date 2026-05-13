# tests/conftest.py
"""
Conftest: mock preflight.check at collection time so importing workspace.py
during test collection does not trigger real platform detection.
"""
from unittest.mock import patch

# Started before any test file is imported. Ensures workspace.py can be
# imported safely in all environments (CI, Docker, no-tmux machines).
_preflight_check_patcher = patch("scripts.preflight.check")
_preflight_check_patcher.start()
