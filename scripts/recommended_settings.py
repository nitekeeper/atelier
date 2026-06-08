"""Recommended cost-posture settings + version-upgrade eligibility.

This module is the SINGLE SOURCE OF TRUTH for the opt-in, consent-gated
"apply the recommended cost settings on a version upgrade" feature.

When atelier's plugin version is bumped, the FIRST session on the new
version may OFFER (default No) to apply a cost-optimized posture to the
user's global ``~/.claude/settings.json``:

    {"model": "sonnet", "effortLevel": "high", "autoCompactEnabled": true}

The model field uses the version-resilient family ALIAS ``"sonnet"`` (the
``model`` field and the Agent tool accept aliases), NOT a pinned
``claude-sonnet-4-6`` id — so the recommendation survives model
re-versioning without a code change.

Design constraints (load-bearing — this writes to a user's GLOBAL settings):

  * **Pure + testable.** No interactive I/O lives here; the consent y/N
    prompt belongs to the orchestrating skill
    (``internal/settings-recommendation/SKILL.md``). Python computes and
    (only on the explicit ``apply_recommended`` call) writes.
  * **Read-only compute paths.** ``eligibility`` / ``maybe_offer`` /
    ``compute_changes`` / ``load_settings`` / ``read_state`` NEVER write.
    Only ``apply_recommended`` and ``write_state`` mutate disk.
  * **Merge-safe.** ``apply_recommended`` sets ONLY the three recommended
    keys, preserving EVERY existing top-level key (``env``,
    ``enabledPlugins``, ``permissions``, ``statusLine``, ``hooks`` …).
  * **Atomic.** Writes go through a temp file in the same dir + ``os.replace``
    so a reader never sees a partial file and no ``.tmp`` debris remains.
  * **Idempotent.** ``compute_changes`` returns ``{}`` once the recommended
    keys already match; a re-apply is a no-op.
  * **Never crashes a session.** Missing / malformed files degrade to ``{}``
    on read; ``apply_recommended`` catches file errors and returns ``{}``
    rather than raising.
  * **Never writes managed-settings.json.** Only the user
    ``~/.claude/settings.json`` is ever touched; enterprise-managed policy is
    out of scope (and may override these — the offer text says so).

Env overrides (for hermetic tests + non-default installs):

  * ``CLAUDE_SETTINGS_PATH`` — overrides the settings.json target path.
  * ``ATELIER_SETTINGS_REC_STATE_PATH`` — overrides the state file path.
  * ``ATELIER_STATE_DIR`` — overrides only the state DIRECTORY (state file is
    ``<dir>/settings_rec_state.json``); mirrors ``scripts/scope.py``'s
    ``.atelier`` state-dir convention.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# ── The single canonical source of truth ──────────────────────────────────────
#
# The cost-optimized opt-in posture. ``model`` is the family ALIAS "sonnet"
# (version-resilient), NOT a pinned ``claude-sonnet-4-6`` id — repinning to a
# versioned id is a regression (test_recommended_settings pins this).
RECOMMENDED: dict[str, Any] = {
    "model": "sonnet",
    "effortLevel": "high",
    "autoCompactEnabled": True,
}

_STATE_DIR_NAME = ".atelier"  # mirrors scripts/scope.py._STATE_DIR_NAME
_STATE_FILE_NAME = "settings_rec_state.json"


# ── Path resolution (env-overridable; no hardcoded /home/<user>) ───────────────


def settings_path() -> Path:
    """Resolve the user's global Claude settings file.

    ``CLAUDE_SETTINGS_PATH`` env override (hermetic tests + non-default
    installs) wins; otherwise ``~/.claude/settings.json`` via ``Path.home()``.
    """
    override = os.environ.get("CLAUDE_SETTINGS_PATH")
    if override and override.strip():
        return Path(override)
    return Path.home() / ".claude" / "settings.json"


def state_path() -> Path:
    """Resolve the per-version state file ``~/.atelier/settings_rec_state.json``.

    Resolution order: ``ATELIER_SETTINGS_REC_STATE_PATH`` (full path) →
    ``ATELIER_STATE_DIR`` (directory; file appended) → ``~/.atelier/…``.
    Mirrors ``scripts/scope.py``'s ``.atelier`` state-dir convention.
    """
    override = os.environ.get("ATELIER_SETTINGS_REC_STATE_PATH")
    if override and override.strip():
        return Path(override)
    state_dir = os.environ.get("ATELIER_STATE_DIR")
    if state_dir and state_dir.strip():
        return Path(state_dir) / _STATE_FILE_NAME
    return Path.home() / _STATE_DIR_NAME / _STATE_FILE_NAME


def current_plugin_version() -> str | None:
    """Read the INSTALLED atelier plugin version from its manifest.

    The manifest lives at ``.claude-plugin/plugin.json`` relative to the
    plugin ROOT — resolved as ``Path(__file__).resolve().parents[1]`` (the
    same anchor ``scripts/bootstrap.py`` uses for the atelier plugin root),
    NEVER a ``Path.cwd()`` walk (CWD is the user's target project, not the
    plugin). Returns ``None`` on missing / malformed manifest so a fresh or
    broken install never crashes the pre-flight.
    """
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, str) and version.strip():
        return version
    return None


# ── Settings read + change computation (read-only) ─────────────────────────────


def load_settings(path: Path) -> dict:
    """Parse the settings JSON at ``path``; return ``{}`` on a missing file OR
    malformed JSON. NEVER raises — a broken settings.json must not crash a
    session's pre-flight.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def compute_changes(current: dict, recommended: dict | None = None) -> dict:
    """Return only the recommended keys whose value is ABSENT or DIFFERS from
    ``current``. ``{}`` when the recommended posture is already applied
    (idempotent). Read-only — never mutates either argument.
    """
    rec = RECOMMENDED if recommended is None else recommended
    return {k: v for k, v in rec.items() if current.get(k) != v}


# ── Per-version state (the only mutators besides apply_recommended) ────────────


def read_state() -> dict:
    """Read the per-version state file; ``{}`` on missing / malformed. Read-only."""
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(version: str, decision: str) -> dict:
    """Record that ``version`` was handled with ``decision`` ∈ {'applied',
    'declined'} so the SAME version never re-prompts. Creates the state dir if
    missing, then writes ATOMICALLY (temp file + ``os.replace``). Returns the
    persisted payload.

    ``decision`` is validated against {'applied', 'declined'} to keep the state
    file well-formed (a typo would silently break the never-re-prompt
    semantics).
    """
    if decision not in ("applied", "declined"):
        raise ValueError(f"decision must be 'applied' or 'declined' (got {decision!r})")
    payload = {"last_handled_version": version, "decision": decision}
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, payload)
    return payload


# ── Eligibility / offer (read-only) ────────────────────────────────────────────


def eligibility() -> dict | None:
    """Return the offer payload iff a version upgrade leaves recommended changes
    to make, else ``None``. READ-ONLY — consults the manifest, settings, and
    state file without writing any of them.

    Eligible iff ALL hold:
      1. ``current_plugin_version()`` is not None (manifest readable), AND
      2. it differs from ``read_state()['last_handled_version']`` (a bump or a
         first-ever run), AND
      3. ``compute_changes(load_settings(...))`` is non-empty (there is
         actually something to apply — already-applied is silent).

    Payload: ``{"eligible": True, "current_version": <ver>, "changes": <dict>}``.
    """
    version = current_plugin_version()
    if version is None:
        return None
    if read_state().get("last_handled_version") == version:
        return None
    changes = compute_changes(load_settings(settings_path()))
    if not changes:
        return None
    return {"eligible": True, "current_version": version, "changes": changes}


def maybe_offer() -> dict | None:
    """Alias for :func:`eligibility` — the name the startup pre-flight calls.
    READ-ONLY: returns the offer payload or ``None``; never mutates disk.
    """
    return eligibility()


# ── The one explicit write entry point ─────────────────────────────────────────


def apply_recommended(path: Path | None = None) -> dict:
    """MERGE the recommended keys into the user's settings.json, preserving
    EVERY existing top-level key, written ATOMICALLY. Returns the changes dict
    that was applied (``{}`` if already applied = no-op).

    Merge-safety: reads the current settings (or ``{}`` if missing/malformed),
    overlays ONLY ``compute_changes`` (the recommended keys that are absent or
    differ), and writes the merged dict back. ``env``, ``enabledPlugins``,
    ``permissions``, ``statusLine``, ``hooks`` and every other top-level key
    are carried through untouched.

    Robustness: a fresh ``~/.claude`` is created (mkdir parents). Any file
    error is caught and surfaced as ``{}`` rather than crashing the session —
    this runs inside a startup pre-flight where a write failure must degrade,
    not abort.
    """
    target = path if path is not None else settings_path()
    current = load_settings(target)
    changes = compute_changes(current)
    if not changes:
        return {}
    merged = dict(current)
    merged.update(changes)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, merged)
    except OSError:
        # Never crash a session on a settings write failure — degrade to no-op.
        return {}
    return changes


# ── Atomic JSON write helper ───────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Serialize ``payload`` to ``path`` atomically: temp file in the SAME dir
    → ``os.replace``. A reader never sees a partial file and no ``.tmp`` debris
    remains on success. On any failure the orphan temp file is best-effort
    removed and the original exception re-raised.
    """
    fd, tmp = tempfile.mkstemp(prefix=".settings-rec-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
