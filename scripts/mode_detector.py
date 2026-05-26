"""Detect whether Memex v2 is installed + reachable.

Result is cached for the lifetime of the Python process. Each Atelier
command invocation re-imports and therefore re-detects.

Note: _cached is process-global without locking. Concurrent threads may
each run detection once before the cache populates; the result is the
same so the wasted work is benign.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

Mode = Literal["memex", "local"]
_InstallStatus = Literal["absent", "ok", "too_old"]

_cached: Mode | None = None
_warned_too_old: bool = False


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


def _check_memex_install_status() -> tuple[_InstallStatus, str | None]:
    """Detect Memex installation state at v2.2.0-floor granularity (atelier#35).

    Returns:
      - ``("absent", None)`` — Memex not installed (or manifest unreadable/malformed
        such that we can't even read a version string). Local mode is intentional.
      - ``("ok", None)`` — Memex installed AND meets the v2.2.0 API floor.
      - ``("too_old", "<version-string>")`` — Memex installed and identifiable
        BUT its version is below ``_MEMEX_API_FLOOR``. Returned version string is
        the raw value read from ``.claude-plugin/plugin.json``, used by the
        once-per-process warning emitted from ``detect_mode()``.

    The split between "absent" and "too_old" is what lets us be quiet in the
    install-Memex-when-ready case (no signal) while surfacing a single user-
    visible hint in the installed-but-stale case (UX polish; degradation is
    correct either way).
    """
    config = _memex_home() / "config.json"
    if not config.exists():
        return ("absent", None)
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("absent", None)
    if not isinstance(data, dict):
        return ("absent", None)
    plugin_root = data.get("plugin_root")
    if not isinstance(plugin_root, str) or not plugin_root.strip():
        return ("absent", None)
    if not Path(plugin_root).is_absolute():
        return ("absent", None)
    root = Path(plugin_root)
    if not root.is_dir():
        return ("absent", None)
    manifest = root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return ("absent", None)
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("absent", None)
    if not isinstance(manifest_data, dict):
        return ("absent", None)
    if manifest_data.get("name") != "memex":
        return ("absent", None)
    version_str = manifest_data.get("version", "")
    version = _parse_version_tuple(version_str)
    if version is None:
        # Manifest exists and names memex but version is unparseable — treat
        # as "absent" so we don't false-positive the too-old warning on a
        # malformed install. (Pre-release tags like "2.5.0-rc1" land here
        # per the _parse_version_tuple docstring.)
        return ("absent", None)
    if version < _MEMEX_API_FLOOR:
        return ("too_old", version_str)
    return ("ok", None)


def _memex_plugin_reachable() -> bool:
    """True if Memex is installed and meets the v2.2.0 API floor.

    Delegates the four-stage probe (config.json pin, plugin_root, manifest
    name, version floor) to ``_check_memex_install_status``; this stub
    collapses ``"absent"`` and ``"too_old"`` to False for the existing
    ``detect_mode`` callsite that doesn't care about the distinction.
    """
    status, _ = _check_memex_install_status()
    return status == "ok"


def _emit_too_old_warning_once(version_str: str) -> None:
    """Emit a single once-per-process stderr line when Memex is present but
    below the v2.2.0 floor (atelier#35). Rate-limited via the module-global
    ``_warned_too_old`` flag; ``_clear_cache()`` resets it for tests.

    Local mode degradation works correctly either way — this is purely
    about making the silent-fallback visible to the operator.
    """
    global _warned_too_old
    if _warned_too_old:
        return
    _warned_too_old = True
    floor = ".".join(str(p) for p in _MEMEX_API_FLOOR)
    print(
        f"atelier: Memex v{version_str} detected, but atelier requires "
        f"v{floor}+ for Memex mode. Running in Local mode. "
        f"Upgrade Memex via your installer (e.g. `/agora:install memex`).",
        file=sys.stderr,
    )


def detect_mode() -> Mode:
    """Return "memex" if Memex v2 is installed and reachable, else "local".

    Result is cached per-process; call _clear_cache() to force re-detection (tests only).

    When Memex is installed but below the v2.2.0 floor (atelier#35), emit a
    single once-per-process stderr hint before degrading to Local mode. The
    hint is suppressed when Memex is absent entirely (Local mode is intentional).
    """
    global _cached
    if _cached is not None:
        return _cached
    home = _memex_home()
    if not home.exists() or not (home / "registry.json").exists() or not _memex_plugin_reachable():
        _cached = "local"
        # atelier#35 — distinguish "absent" from "too_old" so we can surface
        # a single user-visible hint in the stale-install case. Only run the
        # finer-grained probe when the cheap reachable() gate already failed
        # AND the registry IS present (suppresses the warning when Memex is
        # intentionally absent).
        if home.exists() and (home / "registry.json").exists():
            status, version_str = _check_memex_install_status()
            if status == "too_old" and version_str is not None:
                _emit_too_old_warning_once(version_str)
    else:
        _cached = "memex"
    return _cached


def _clear_cache() -> None:
    """Test-only — force re-detection."""
    global _cached, _warned_too_old
    _cached = None
    _warned_too_old = False
