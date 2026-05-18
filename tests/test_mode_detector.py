import json
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts import mode_detector


@pytest.fixture(autouse=True)
def clear_cache():
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
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=False):
        assert mode_detector.detect_mode() == "local"


def test_returns_memex_when_both_present(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True):
        assert mode_detector.detect_mode() == "memex"


def test_result_is_cached(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True) as m:
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        assert m.call_count == 1, "second call must hit cache"


def test_clear_cache_recomputes(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True):
        mode_detector.detect_mode()
    mode_detector._clear_cache()
    with patch("scripts.mode_detector._memex_home", return_value=tmp_path / "absent"):
        assert mode_detector.detect_mode() == "local"


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


def test_plugin_reachable_false_when_pinned_root_missing(tmp_path):
    """Stale pin — Memex was uninstalled but config.json wasn't cleaned up."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps(
        {"plugin_root": str(tmp_path / "deleted-cache-entry")}
    ))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_wrong_name(tmp_path):
    """Defensive — the pin points at a real plugin, just not the memex one."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "some-other-plugin"
    plugin_root.mkdir()
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "not-memex"})
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
