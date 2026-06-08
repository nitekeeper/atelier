"""Tests for ``scripts.recommended_settings`` — the cost-posture settings +
version-upgrade eligibility module (the single source of truth for the
consent-gated "apply recommended cost settings on a version bump" feature).

All tests are HERMETIC: ``CLAUDE_SETTINGS_PATH`` and
``ATELIER_SETTINGS_REC_STATE_PATH`` are monkeypatched into ``tmp_path`` so no
real ``~/.claude`` or ``~/.atelier`` file is read or written.

The assertions encode the load-bearing invariants and FAIL on a silent revert:
merge-safety, idempotency, version-gating, missing/malformed graceful read,
read-only compute paths, atomic write, and the canonical-constant pin.
"""

from __future__ import annotations

import json

import pytest

from scripts import recommended_settings as rs


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point settings + state at tmp files; yield their paths."""
    settings = tmp_path / "claude" / "settings.json"
    state = tmp_path / "atelier" / "settings_rec_state.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("ATELIER_SETTINGS_REC_STATE_PATH", str(state))
    # Defensive: clear the dir-only override so it can't leak in.
    monkeypatch.delenv("ATELIER_STATE_DIR", raising=False)
    return settings, state


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── (h) CONSTANT pin ───────────────────────────────────────────────────────────


def test_recommended_constant_is_pinned():
    """The canonical RECOMMENDED dict is exactly the cost-optimized triple AND
    model is the family ALIAS 'sonnet' (NOT a pinned claude-sonnet-* id).

    ANTI-REVERT: fails if someone changes a value OR repins model to a
    versioned id (e.g. 'claude-sonnet-4-6')."""
    assert rs.RECOMMENDED == {
        "model": "sonnet",
        "effortLevel": "high",
        "autoCompactEnabled": True,
    }
    assert rs.RECOMMENDED["model"] == "sonnet"
    # The alias must not be a pinned claude-sonnet-<ver> id.
    assert not rs.RECOMMENDED["model"].startswith("claude-")


# ── (a) MERGE-SAFETY anti-revert ───────────────────────────────────────────────


def test_apply_preserves_all_existing_keys(hermetic):
    """apply_recommended adds ONLY the 3 recommended keys and leaves every
    pre-existing top-level key byte-identical.

    ANTI-REVERT: fails if a future change clobbers/overwrites the whole file
    instead of merging."""
    settings, _ = hermetic
    pre = {
        "env": {"FOO": "bar", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "enabledPlugins": ["atelier", "memex"],
        "permissions": {"allow": ["Bash(git status)"]},
        "statusLine": {"type": "command", "command": "echo hi"},
    }
    _write_json(settings, pre)

    changes = rs.apply_recommended()
    assert changes == rs.RECOMMENDED  # all 3 were absent → all applied

    after = json.loads(settings.read_text(encoding="utf-8"))
    # Every pre-existing key survives byte-for-byte.
    for k, v in pre.items():
        assert after[k] == v
    # Exactly the recommended keys were added, nothing else.
    assert set(after) == set(pre) | set(rs.RECOMMENDED)
    for k, v in rs.RECOMMENDED.items():
        assert after[k] == v


# ── (b) IDEMPOTENCY ────────────────────────────────────────────────────────────


def test_apply_is_idempotent(hermetic):
    """A second compute_changes / apply is empty once applied (no-op re-apply)."""
    settings, _ = hermetic
    _write_json(settings, {"env": {"X": "1"}})

    first = rs.apply_recommended()
    assert first == rs.RECOMMENDED

    current = rs.load_settings(settings)
    assert rs.compute_changes(current) == {}
    assert rs.apply_recommended() == {}


# ── (c) VERSION-GATING ─────────────────────────────────────────────────────────


def test_version_gating(hermetic, monkeypatch):
    """After write_state(v, decision), maybe_offer() is None for v; bumping the
    plugin version makes it eligible again."""
    settings, _ = hermetic
    _write_json(settings, {"env": {"X": "1"}})  # changes are pending

    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")
    # Fresh: no state, changes pending → eligible.
    offer = rs.maybe_offer()
    assert offer is not None
    assert offer["eligible"] is True
    assert offer["current_version"] == "1.5.0"
    assert offer["changes"] == rs.RECOMMENDED

    # Decide (declined): same version must not re-prompt.
    rs.write_state("1.5.0", "declined")
    assert rs.maybe_offer() is None

    # A NEW version re-offers (changes still pending — settings untouched on decline).
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.6.0")
    again = rs.maybe_offer()
    assert again is not None
    assert again["current_version"] == "1.6.0"


def test_version_gating_applied_also_records(hermetic, monkeypatch):
    """After write_state(v, 'applied'), same version is not eligible."""
    settings, _ = hermetic
    _write_json(settings, {"env": {"X": "1"}})
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")

    assert rs.maybe_offer() is not None
    rs.write_state("1.5.0", "applied")
    assert rs.maybe_offer() is None


def test_eligibility_none_when_already_applied(hermetic, monkeypatch):
    """Even on a brand-new version, an already-applied posture is silent (no
    offer) — the changes set is empty."""
    settings, _ = hermetic
    _write_json(settings, dict(rs.RECOMMENDED))  # already applied
    monkeypatch.setattr(rs, "current_plugin_version", lambda: "9.9.9")
    assert rs.maybe_offer() is None


def test_eligibility_none_when_version_unreadable(hermetic, monkeypatch):
    """A None plugin version (missing/malformed manifest) yields no offer."""
    settings, _ = hermetic
    _write_json(settings, {"env": {"X": "1"}})
    monkeypatch.setattr(rs, "current_plugin_version", lambda: None)
    assert rs.maybe_offer() is None


# ── (d) MISSING settings.json ──────────────────────────────────────────────────


def test_missing_settings_file(hermetic):
    """Missing settings.json → load_settings == {}; apply creates the file
    containing exactly RECOMMENDED."""
    settings, _ = hermetic
    assert not settings.exists()
    assert rs.load_settings(settings) == {}

    changes = rs.apply_recommended()
    assert changes == rs.RECOMMENDED
    assert settings.exists()
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after == rs.RECOMMENDED


# ── (e) MALFORMED settings.json ────────────────────────────────────────────────


def test_malformed_settings_file_graceful(hermetic):
    """Invalid JSON → load_settings == {} and apply does not raise."""
    settings, _ = hermetic
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not valid json,,,", encoding="utf-8")

    assert rs.load_settings(settings) == {}
    # apply treats it as {} and writes the recommended set (no raise).
    changes = rs.apply_recommended()
    assert changes == rs.RECOMMENDED
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after == rs.RECOMMENDED


# ── (f) CONSENT / READ-ONLY anti-revert ────────────────────────────────────────


def test_compute_paths_never_write(hermetic, monkeypatch):
    """eligibility() / maybe_offer() / compute_changes() leave settings.json and
    the state file BYTE-UNCHANGED. Only apply_recommended / write_state mutate.

    ANTI-REVERT: fails if a write ever leaks into a compute/eligibility path."""
    settings, state = hermetic
    pre_settings = {"env": {"X": "1"}}
    _write_json(settings, pre_settings)
    _write_json(state, {"last_handled_version": "0.0.1", "decision": "declined"})

    settings_bytes = settings.read_bytes()
    state_bytes = state.read_bytes()

    monkeypatch.setattr(rs, "current_plugin_version", lambda: "1.5.0")
    rs.eligibility()
    rs.maybe_offer()
    rs.compute_changes(rs.load_settings(settings))
    rs.read_state()

    assert settings.read_bytes() == settings_bytes
    assert state.read_bytes() == state_bytes


# ── (g) ATOMIC write ───────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_partial_file(hermetic):
    """After apply, the target dir contains exactly settings.json — no .tmp /
    partial file lingers."""
    settings, _ = hermetic
    rs.apply_recommended()
    leftovers = sorted(p.name for p in settings.parent.iterdir())
    assert leftovers == ["settings.json"]
    assert not any(p.name.endswith(".tmp") for p in settings.parent.iterdir())


def test_write_state_atomic_no_partial(hermetic):
    """write_state likewise leaves no .tmp debris."""
    _, state = hermetic
    rs.write_state("1.5.0", "applied")
    leftovers = sorted(p.name for p in state.parent.iterdir())
    assert leftovers == ["settings_rec_state.json"]


# ── state read/write round-trip + validation ───────────────────────────────────


def test_state_roundtrip(hermetic):
    assert rs.read_state() == {}  # missing → {}
    rs.write_state("2.0.0", "applied")
    assert rs.read_state() == {"last_handled_version": "2.0.0", "decision": "applied"}


def test_write_state_rejects_bad_decision(hermetic):
    with pytest.raises(ValueError):
        rs.write_state("1.5.0", "maybe")


def test_read_state_malformed_graceful(hermetic):
    _, state = hermetic
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("garbage{{", encoding="utf-8")
    assert rs.read_state() == {}


# ── state_path env-override precedence (ATELIER_STATE_DIR) ──────────────────────


def test_state_path_dir_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_SETTINGS_REC_STATE_PATH", raising=False)
    monkeypatch.setenv("ATELIER_STATE_DIR", str(tmp_path / "custom"))
    assert rs.state_path() == tmp_path / "custom" / "settings_rec_state.json"


# ── current_plugin_version reads the real manifest ─────────────────────────────


def test_current_plugin_version_reads_manifest():
    """The live manifest version is a parseable X.Y.Z string (smoke that the
    resolver anchors on the plugin root, not CWD)."""
    ver = rs.current_plugin_version()
    assert ver is not None
    parts = ver.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
