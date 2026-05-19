"""Detect whether Memex v2 is installed + reachable.

Result is cached for the lifetime of the Python process. Each Atelier
command invocation re-imports and therefore re-detects.

Note: _cached is process-global without locking. Concurrent threads may
each run detection once before the cache populates; the result is the
same so the wasted work is benign.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Literal

Mode = Literal["memex", "local"]

_cached: Mode | None = None


def _memex_home() -> Path:
    return Path.home() / ".memex"


_MEMEX_API_FLOOR = (2, 2, 0)  # spec §6 prerequisite — caller-built librarian_output


def _parse_version_tuple(s: str) -> tuple[int, int, int] | None:
    """Parse a "X.Y.Z" string into a (major, minor, patch) tuple of ints.
    Returns None if the string isn't parseable (e.g., empty, malformed).
    Trailing components are ignored; only major.minor.patch are read."""
    if not isinstance(s, str):
        return None
    parts = s.strip().split(".")
    if len(parts) < 3:
        return None
    try:
        # Pre-releases (e.g. "2.5.0-rc1") are intentionally rejected; track if Memex starts shipping -rcN.
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _memex_plugin_reachable() -> bool:
    """True if Memex itself has resolved + pinned its plugin root in
    ~/.memex/config.json (Memex v2.5.0+ contract — see spec §4.2 rationale)
    AND the pinned memex version meets the v2.2.0 API floor (spec §6
    prerequisite — caller-built `librarian_output`).

    The pin is written by Memex's Step 0.2 preflight after it has already
    validated that the plugin directory exists, scripts/install.py is a
    regular file, and .claude-plugin/plugin.json declares name="memex".
    Atelier re-verifies the manifest because the cached pin could go
    stale between Memex invocations (e.g., a version directory was
    deleted during cache cleanup), AND re-checks the version because a
    user could (in theory) downgrade memex below atelier's API floor.
    Returning False on under-floor versions degrades to local mode
    rather than crashing on the first Tier 2 write.
    """
    config = _memex_home() / "config.json"
    if not config.exists():
        return False
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    plugin_root = data.get("plugin_root")
    if not isinstance(plugin_root, str) or not plugin_root.strip():
        return False
    if not Path(plugin_root).is_absolute():
        return False
    root = Path(plugin_root)
    if not root.is_dir():
        return False
    manifest = root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(manifest_data, dict):
        return False
    if manifest_data.get("name") != "memex":
        return False
    version = _parse_version_tuple(manifest_data.get("version", ""))
    if version is None or version < _MEMEX_API_FLOOR:
        return False
    return True


def detect_mode() -> Mode:
    """Return "memex" if Memex v2 is installed and reachable, else "local".

    Result is cached per-process; call _clear_cache() to force re-detection (tests only).
    """
    global _cached
    if _cached is not None:
        return _cached
    home = _memex_home()
    if not home.exists() or not (home / "registry.json").exists():
        _cached = "local"
    elif not _memex_plugin_reachable():
        _cached = "local"
    else:
        _cached = "memex"
    return _cached


def _clear_cache() -> None:
    """Test-only — force re-detection."""
    global _cached
    _cached = None
