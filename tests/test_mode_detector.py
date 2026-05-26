import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import mode_detector
from scripts.mode_detector import _parse_version_tuple


@pytest.fixture(autouse=True)
def auto_clear_cache():
    mode_detector._clear_cache()
    yield
    mode_detector._clear_cache()


def test_returns_local_when_no_memex_home(tmp_path):
    with patch("scripts.mode_detector._memex_home", return_value=tmp_path / "absent"):
        assert mode_detector.detect_mode() == "local"


def test_returns_local_when_registry_absent(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector.detect_mode() == "local"


def test_returns_local_when_registry_present_but_plugin_unreachable(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with (
        patch("scripts.mode_detector._memex_home", return_value=memex_home),
        patch("scripts.mode_detector._memex_plugin_reachable", return_value=False),
    ):
        assert mode_detector.detect_mode() == "local"


def test_returns_memex_when_both_present(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with (
        patch("scripts.mode_detector._memex_home", return_value=memex_home),
        patch("scripts.mode_detector._memex_plugin_reachable", return_value=True),
    ):
        assert mode_detector.detect_mode() == "memex"


def test_result_is_cached(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with (
        patch("scripts.mode_detector._memex_home", return_value=memex_home),
        patch("scripts.mode_detector._memex_plugin_reachable", return_value=True) as m,
    ):
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        assert m.call_count == 1, "second call must hit cache"


def test_clear_cache_recomputes(tmp_path):
    """Forcing _clear_cache() between calls must cause the second call to
    re-evaluate detection rather than return the stashed result."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with (
        patch("scripts.mode_detector._memex_home", return_value=memex_home),
        patch("scripts.mode_detector._memex_plugin_reachable", side_effect=[True, False]) as m,
    ):
        first = mode_detector.detect_mode()
        mode_detector._clear_cache()
        second = mode_detector.detect_mode()
        assert first == "memex"
        assert second == "local"
        assert m.call_count == 2, "second call must recompute, not hit cache"


def _make_fake_plugin_root(root: Path) -> None:
    """Build the minimum filesystem structure _memex_plugin_reachable() validates."""
    root.mkdir(parents=True, exist_ok=True)
    cp = root / ".claude-plugin"
    cp.mkdir()
    (cp / "plugin.json").write_text(json.dumps({"name": "memex", "version": "2.5.1"}))


def test_plugin_reachable_true_with_valid_config_pin(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.5.1"
    _make_fake_plugin_root(plugin_root)
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is True


def test_plugin_reachable_false_when_config_missing(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_config_invalid_json(tmp_path):
    """Defensive — half-written config.json (e.g., Memex crashed mid-write)."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text("{not-valid-json")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_config_is_array(tmp_path):
    """Defensive — config.json contains a JSON array, not an object.
    `.get()` would crash; the isinstance(dict) guard must short-circuit."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text("[1,2,3]")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_is_array(tmp_path):
    """Defensive — manifest plugin.json contains a JSON array, not an object."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-array-manifest"
    plugin_root.mkdir()
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".claude-plugin" / "plugin.json").write_text("[1,2,3]")
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_plugin_root_is_empty_string(tmp_path):
    """Defensive — empty plugin_root must not fall through to Path("")
    which resolves to CWD and may spuriously look like a valid dir."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": ""}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_plugin_root_is_relative(tmp_path):
    """Defensive — relative plugin_root must be rejected. A relative
    path could resolve unpredictably depending on CWD at detection time."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": "some/relative/path"}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_plugin_root_is_missing_from_config(tmp_path):
    """Defensive — config.json is a valid dict but lacks plugin_root key."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps({}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_plugin_root_is_null(tmp_path):
    """Defensive — plugin_root is explicitly null."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": None}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_plugin_root_is_int(tmp_path):
    """Defensive — plugin_root has the wrong type entirely."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": 42}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_missing(tmp_path):
    """Defensive — plugin_root exists as a directory but the
    .claude-plugin/plugin.json manifest is absent."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-no-manifest"
    plugin_root.mkdir()
    # Intentionally no .claude-plugin/plugin.json
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_malformed_json(tmp_path):
    """Defensive — manifest exists but contains invalid JSON."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-malformed-manifest"
    plugin_root.mkdir()
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".claude-plugin" / "plugin.json").write_text("not-json")
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_pinned_root_missing(tmp_path):
    """Stale pin — Memex was uninstalled but config.json wasn't cleaned up."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(
        json.dumps({"plugin_root": str(tmp_path / "deleted-cache-entry")})
    )
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_wrong_name(tmp_path):
    """Defensive — the pin points at a real plugin, just not the memex one."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "some-other-plugin"
    _make_fake_plugin_root(plugin_root)
    # Override the manifest to declare a non-memex name
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "not-memex", "version": "2.5.1"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_memex_below_api_floor(tmp_path):
    """Defensive — pin points at memex but version < 2.2.0 (no
    caller-built librarian_output). Returns False so atelier degrades
    to local mode rather than crashing on the first Tier 2 write."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.1.0"
    _make_fake_plugin_root(plugin_root)
    # Overwrite the manifest with a sub-floor version
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.1.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_true_when_memex_at_api_floor(tmp_path):
    """Exact-floor version (2.2.0) passes."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.2.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.2.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is True


def test_detect_mode_returns_memex_at_api_floor(tmp_path):
    """End-to-end check that an exact-floor pin makes detect_mode()
    report "memex" — parallels the unit test above but through the
    public API including the registry.json gate."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    plugin_root = tmp_path / "memex-2.2.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.2.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector.detect_mode() == "memex"


def test_plugin_reachable_false_when_manifest_version_unparseable(tmp_path):
    """Defensive — manifest has no version field or malformed value."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-malformed"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex"})  # no version key
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_parse_version_returns_none_on_non_numeric_parts():
    """Unit test for the helper — a non-numeric segment must trigger the
    ValueError branch and yield None rather than crashing."""
    assert _parse_version_tuple("2.x.0") is None


# ──────────────────────────────────────────────────────────────────────────
# atelier#35 — version-floor warning
# ──────────────────────────────────────────────────────────────────────────


def test_check_install_status_absent_when_no_config(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        status, version = mode_detector._check_memex_install_status()
        assert status == "absent"
        assert version is None


def test_check_install_status_ok_at_floor(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.2.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.2.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        status, version = mode_detector._check_memex_install_status()
        assert status == "ok"
        assert version is None


def test_check_install_status_too_old_below_floor(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.1.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.1.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        status, version = mode_detector._check_memex_install_status()
        assert status == "too_old"
        assert version == "2.1.0"


def test_detect_mode_warns_once_when_memex_too_old(tmp_path, capsys):
    """When Memex is installed but below the v2.2.0 floor, detect_mode()
    must emit a single stderr hint and degrade to Local mode."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    plugin_root = tmp_path / "memex-2.1.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.1.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        result = mode_detector.detect_mode()
    assert result == "local"
    captured = capsys.readouterr()
    assert "v2.1.0" in captured.err
    assert "v2.2.0+" in captured.err
    assert "Running in Local mode" in captured.err


def test_detect_mode_silent_when_memex_absent(tmp_path, capsys):
    """No Memex registry at all — Local mode is intentional, no warning."""
    memex_home = tmp_path / ".memex-not-here"
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        result = mode_detector.detect_mode()
    assert result == "local"
    captured = capsys.readouterr()
    assert captured.err == ""


def test_detect_mode_silent_when_memex_at_floor(tmp_path, capsys):
    """Memex installed and meets the floor — no warning, Memex mode."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    plugin_root = tmp_path / "memex-2.2.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.2.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        result = mode_detector.detect_mode()
    assert result == "memex"
    captured = capsys.readouterr()
    assert captured.err == ""


def test_warning_is_rate_limited_per_process(tmp_path, capsys):
    """Multiple detect_mode() calls (even after _clear_cache) within one
    process emit the warning at most once until _clear_cache resets the
    flag. _clear_cache resets BOTH cache and warning flag, so a test that
    explicitly clears can re-trigger the warning."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    plugin_root = tmp_path / "memex-2.1.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.1.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        mode_detector.detect_mode()
        # Cache returns cached value on second call; no extra warning.
        mode_detector.detect_mode()
        # Even with explicit cache clear (which also resets the warning flag
        # for tests), only the resulting fresh probe emits — total 2 warnings
        # across the two probe rounds.
        mode_detector._clear_cache()
        mode_detector.detect_mode()
    captured = capsys.readouterr()
    # Exactly two emissions: one per fresh probe-round.
    assert captured.err.count("v2.1.0 detected") == 2
