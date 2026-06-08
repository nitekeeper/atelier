"""Pin the version-literal agreement the upgrade-detection feature depends on.

`scripts.recommended_settings.current_plugin_version()` reads
`.claude-plugin/plugin.json`; the upgrade offer fires when that value differs
from the recorded `last_handled_version`. If the canonical manifest version and
`scripts.bootstrap._atelier_version()`'s fallback literal drift, the bootstrap
marker re-fires AND the two version sources disagree — so this test asserts they
agree (AI-6).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).parent.parent


def _manifest_version() -> str:
    data = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return data["version"]


def test_plugin_json_and_bootstrap_fallback_agree():
    """plugin.json version == bootstrap _atelier_version() fallback literal.

    ANTI-REVERT: bumping one and not the other (the drift bump.py warns about)
    goes RED here — and the upgrade-detection eligibility depends on this
    agreement."""
    from scripts.bootstrap import _atelier_version

    assert _manifest_version() == _atelier_version()


def test_current_plugin_version_matches_manifest():
    """recommended_settings.current_plugin_version() reads the SAME canonical
    manifest version the bootstrap fallback mirrors."""
    from scripts.recommended_settings import current_plugin_version

    assert current_plugin_version() == _manifest_version()
