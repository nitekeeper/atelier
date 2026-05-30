"""Tests for scripts/tmux_setup.py — consent + idempotency + safety invariants (atelier#63)."""

import io
from pathlib import Path

import pytest

from scripts import preflight, tmux_setup

# Kaizen's marker prefix — the atelier marker MUST NOT equal it, so the two
# tools never match/clobber each other's blocks on a shared machine.
_KAIZEN_MARKER_START = "# >>> agent-teams v{} >>>"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir so the call-time path helpers resolve under it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def tmux_yes(monkeypatch):
    """tmux is available (write-path tests need this gate to pass)."""
    monkeypatch.setattr(preflight, "tmux_available", lambda: True)


def _interactive(monkeypatch, answer: str) -> None:
    """Make stdin a tty answering ``answer`` to the consent prompt."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: answer)


def _non_interactive(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


# ── Happy path: first run writes both files ─────────────────────────────────


def test_first_run_writes_both_files(home, tmux_yes):
    result = tmux_setup.setup_tmux(assume_yes=True)
    assert result == {
        "config_written": True,
        "source_added": True,
        "skipped": False,
        "reason": None,
    }
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    user_conf = home / ".tmux.conf"
    assert atelier_conf.exists()
    body = atelier_conf.read_text(encoding="utf-8")
    # Marker present at current version.
    assert tmux_setup.MARKER_START.format(tmux_setup.MARKER_VERSION) in body
    assert tmux_setup.MARKER_END.format(tmux_setup.MARKER_VERSION) in body
    # The 3 keybindings (p / w / 1..9).
    assert "bind p select-pane -t 0" in body
    assert "bind -r w select-pane -t :.+" in body
    for n in range(1, 10):
        assert f"bind {n} select-pane -t {n}" in body
    # Composite pane-border-format render.
    assert "set -g pane-border-status top" in body
    assert "#{=1:pane_title}" in body
    assert "#{?@desired_title,#{@desired_title},#{pane_title}}" in body
    assert "set -g main-pane-width 60" in body
    # detect-and-source if-shell block.
    assert "if-shell -b '[ -d \"$HOME/.tmux/plugins/tmux-agent-indicator\" ]'" in body
    # User include line.
    user_body = user_conf.read_text(encoding="utf-8")
    assert tmux_setup.SOURCE_LINE in user_body
    assert tmux_setup.SOURCE_LINE_COMMENT in user_body


# ── Idempotency: second run at same version is a full no-op ─────────────────


def test_idempotent_second_run(home, tmux_yes):
    first = tmux_setup.setup_tmux(assume_yes=True)
    assert first["config_written"] and first["source_added"]
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    user_conf = home / ".tmux.conf"
    conf_after_first = atelier_conf.read_text(encoding="utf-8")
    user_after_first = user_conf.read_text(encoding="utf-8")

    second = tmux_setup.setup_tmux(assume_yes=True)
    assert second == {
        "config_written": False,
        "source_added": False,
        "skipped": True,
        "reason": None,
    }
    # Both files byte-identical after the second run.
    assert atelier_conf.read_text(encoding="utf-8") == conf_after_first
    assert user_conf.read_text(encoding="utf-8") == user_after_first
    # No backup file was created (the include line was already present).
    backups = list(home.glob(".tmux.conf.bak.*"))
    assert backups == []


# ── apply_config_block is byte-identical-idempotent at the same version ─────


def test_apply_config_block_double_apply_byte_identical(home):
    """Two applies at MARKER_VERSION with no intervening edit are byte-identical.

    Regression: when the file contained ONLY the block (the create output),
    ``_strip_existing_block`` returns a lone ``\\n`` whose truthiness used to
    fire the ``\\n`` separator branch, prepending two blank lines on the second
    apply (1865 → 1867 bytes) before stabilizing. The strip remnant is now
    treated as empty, so create-then-reapply leaves the file untouched.
    """
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    after_first = atelier_conf.read_bytes()
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    after_second = atelier_conf.read_bytes()
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    after_third = atelier_conf.read_bytes()
    assert after_second == after_first
    assert after_third == after_first
    # No leading blank-line accretion: the file starts at the START marker.
    assert after_first.startswith(
        tmux_setup.MARKER_START.format(tmux_setup.MARKER_VERSION).encode("utf-8")
    )


def test_apply_config_block_double_apply_byte_identical_with_user_content(home):
    """Re-apply over a file that also has surrounding user content is byte-identical."""
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    atelier_conf.parent.mkdir(parents=True, exist_ok=True)
    atelier_conf.write_text("# user above\n\nset -g mouse on\n", encoding="utf-8")
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    after_first = atelier_conf.read_bytes()
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    assert atelier_conf.read_bytes() == after_first
    # User content is still there and there is exactly one block.
    body = after_first.decode("utf-8")
    assert "# user above" in body
    assert "set -g mouse on" in body
    assert body.count(tmux_setup._MARKER_START_PREFIX) == 1


# ── Version bump replaces the block in place, preserving surrounding content ─


def test_version_bump_replaces_in_place(home, tmux_yes):
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    atelier_conf.parent.mkdir(parents=True, exist_ok=True)
    # Seed an OLDER-version block with surrounding user content.
    old_block = (
        f"{tmux_setup.MARKER_START.format(0)}\n"
        "set -g pane-border-status top\n"
        f"{tmux_setup.MARKER_END.format(0)}\n"
    )
    atelier_conf.write_text(
        "# user content above\n\n" + old_block + "\n# user content below\n",
        encoding="utf-8",
    )

    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    body = atelier_conf.read_text(encoding="utf-8")
    # Exactly one current-version START marker, zero old-version markers.
    assert body.count(tmux_setup.MARKER_START.format(tmux_setup.MARKER_VERSION)) == 1
    assert tmux_setup.MARKER_START.format(0) not in body
    # Surrounding user content preserved.
    assert "# user content above" in body
    assert "# user content below" in body
    # Only one block total.
    assert body.count(tmux_setup._MARKER_START_PREFIX) == 1


# ── Malformed marker raises ValueError ──────────────────────────────────────


def test_malformed_marker_version_disagree(home):
    path = home / "conf"
    path.write_text(
        f"{tmux_setup.MARKER_START.format(2)}\nx\n{tmux_setup.MARKER_END.format(3)}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        tmux_setup.detect_existing_marker(path)


def test_malformed_marker_non_numeric(home):
    path = home / "conf"
    path.write_text(
        "# >>> atelier agent-teams vNOPE >>>\nx\n# <<< atelier agent-teams vNOPE <<<\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        tmux_setup.detect_existing_marker(path)


# ── Consent declined → zero writes ──────────────────────────────────────────


def test_consent_declined_zero_writes(home, tmux_yes, monkeypatch):
    _interactive(monkeypatch, "n")
    result = tmux_setup.setup_tmux(assume_yes=False)
    assert result["skipped"] is True
    assert result["reason"] == "declined"
    assert not (home / ".config" / "atelier" / "tmux.conf").exists()
    assert not (home / ".tmux.conf").exists()


def test_consent_explicit_yes_writes(home, tmux_yes, monkeypatch):
    _interactive(monkeypatch, "y")
    result = tmux_setup.setup_tmux(assume_yes=False)
    assert result["config_written"] is True
    assert (home / ".config" / "atelier" / "tmux.conf").exists()


# ── Non-interactive stdin → skip, no writes ─────────────────────────────────


def test_non_interactive_skips(home, tmux_yes, monkeypatch):
    _non_interactive(monkeypatch)
    result = tmux_setup.setup_tmux(assume_yes=False)
    assert result["skipped"] is True
    assert result["reason"] == "non-interactive"
    assert not (home / ".config" / "atelier" / "tmux.conf").exists()
    assert not (home / ".tmux.conf").exists()


# ── Gate: not tmux / not agent-team → no-op fallback ────────────────────────


def test_gate_no_tmux_skips(home, monkeypatch):
    monkeypatch.setattr(preflight, "tmux_available", lambda: False)
    result = tmux_setup.setup_tmux(assume_yes=True)
    assert result == {
        "config_written": False,
        "source_added": False,
        "skipped": True,
        "reason": "not-agent-team-or-no-tmux",
    }
    assert not (home / ".config" / "atelier" / "tmux.conf").exists()


def test_gate_wrong_mode_skips(home, tmux_yes):
    result = tmux_setup.setup_tmux(assume_yes=True, mode="subagent")
    assert result["skipped"] is True
    assert result["reason"] == "not-agent-team-or-no-tmux"
    assert not (home / ".config" / "atelier" / "tmux.conf").exists()


def test_gate_agent_team_mode_proceeds(home, tmux_yes):
    result = tmux_setup.setup_tmux(assume_yes=True, mode="agent-team")
    assert result["config_written"] is True


# ── GAP TEST: config current but source line missing → add ONLY source line ─


def test_source_line_added_when_config_current(home, tmux_yes):
    # Pre-create the atelier config at the CURRENT version...
    atelier_conf = home / ".config" / "atelier" / "tmux.conf"
    tmux_setup.apply_config_block(atelier_conf, tmux_setup.MARKER_VERSION)
    # ...but with the source line MISSING from ~/.tmux.conf.
    assert not (home / ".tmux.conf").exists()

    result = tmux_setup.setup_tmux(assume_yes=True)
    # Config step no-ops (already current) but source step still fires.
    assert result["config_written"] is False
    assert result["source_added"] is True
    assert result["skipped"] is False
    user_body = (home / ".tmux.conf").read_text(encoding="utf-8")
    assert tmux_setup.SOURCE_LINE in user_body


# ── Backup correctness ──────────────────────────────────────────────────────


def test_add_source_line_backs_up_existing(home):
    user_conf = home / ".tmux.conf"
    original = "set -g mouse on\n# my own config\n"
    user_conf.write_text(original, encoding="utf-8")

    wrote = tmux_setup.add_source_line(user_conf)
    assert wrote is True
    backups = list(home.glob(".tmux.conf.bak.*"))
    assert len(backups) == 1
    # Backup is byte-identical to the pre-modification content.
    assert backups[0].read_text(encoding="utf-8") == original
    # Original content preserved + the source line appended (append-only).
    new_body = user_conf.read_text(encoding="utf-8")
    assert new_body.startswith(original)
    assert tmux_setup.SOURCE_LINE in new_body


def test_add_source_line_idempotent_no_backup(home):
    user_conf = home / ".tmux.conf"
    user_conf.write_text(f"{tmux_setup.SOURCE_LINE}\n", encoding="utf-8")
    wrote = tmux_setup.add_source_line(user_conf)
    assert wrote is False
    assert list(home.glob(".tmux.conf.bak.*")) == []


def test_source_line_present_tolerates_alias_form(home):
    user_conf = home / ".tmux.conf"
    user_conf.write_text("  source ~/.config/atelier/tmux.conf  \n", encoding="utf-8")
    assert tmux_setup.source_line_present(user_conf) is True


def test_source_line_present_missing_file_false(home):
    assert tmux_setup.source_line_present(home / "nope.conf") is False


# ── HARD INVARIANT 1: settings.json is never written / referenced ───────────


def test_settings_json_never_in_config_block():
    assert "settings.json" not in tmux_setup.CONFIG_BLOCK
    assert "settings.json" not in tmux_setup._full_block(tmux_setup.MARKER_VERSION)


def test_no_path_the_module_writes_contains_settings_json(home, tmux_yes):
    tmux_setup.setup_tmux(assume_yes=True)
    # Walk every file under HOME the module could have created.
    for p in home.rglob("*"):
        assert "settings.json" not in str(p)
    # And the canonical module path constants/helpers never name it.
    assert "settings.json" not in str(tmux_setup._atelier_conf_path())
    assert "settings.json" not in str(tmux_setup._user_conf_path())
    assert "settings.json" not in str(tmux_setup.ATELIER_TMUX_CONF)
    assert "settings.json" not in str(tmux_setup.USER_TMUX_CONF)


# ── HARD INVARIANT 2: no allow-passthrough / allow-rename in the block ──────


def test_no_allow_passthrough_or_allow_rename():
    block = tmux_setup._full_block(tmux_setup.MARKER_VERSION)
    assert "allow-passthrough" not in block
    assert "allow-rename" not in block


# ── detect-and-source EMISSION (Python can only assert the string is present) ─


def test_emits_if_shell_guard_and_unconditional_border_format():
    block = tmux_setup.CONFIG_BLOCK
    # The byte-faithful if-shell guard string is present.
    guard = "if-shell -b '[ -d \"$HOME/.tmux/plugins/tmux-agent-indicator\" ]'"
    assert guard in block
    assert "source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux'" in block
    assert "@agent-indicator-icons" in block
    assert "claude=" in block
    # The pane-border-format line is OUTSIDE the if-shell guard (it precedes it).
    border_idx = block.index("set -g pane-border-format")
    guard_idx = block.index(guard)
    assert border_idx < guard_idx


# ── Atelier marker is DISTINCT from kaizen's ────────────────────────────────


def test_atelier_marker_distinct_from_kaizen():
    assert "atelier" in tmux_setup.MARKER_START
    assert "atelier" in tmux_setup.MARKER_END
    assert tmux_setup.MARKER_START != _KAIZEN_MARKER_START
    # The atelier marker prefix must not be a prefix kaizen's detector would
    # match (kaizen looks for "# >>> agent-teams v"; atelier prepends "atelier ").
    assert not tmux_setup.MARKER_START.startswith("# >>> agent-teams v")


# ── The module never shells out to curl / install.sh ────────────────────────


def test_module_never_runs_curl_or_installsh():
    # The writer never installs the plugin: it has NO process-spawning surface
    # at all (its only writes are file writes). We assert behaviorally — the
    # module never imports/binds subprocess, os.system, or os.popen — rather
    # than substring-scanning the source, because the safety COMMENTS legitimately
    # say "never curls install.sh" and would defeat a naive grep.
    import inspect

    assert not hasattr(tmux_setup, "subprocess")
    # `os` IS imported (os.replace, os.fdopen) but the dangerous spawn helpers
    # must never appear in the module body.
    src = inspect.getsource(tmux_setup)
    assert "os.system" not in src
    assert "os.popen" not in src
    # The generated tmux block carries no curl / install.sh either.
    block = tmux_setup._full_block(tmux_setup.MARKER_VERSION)
    assert "curl" not in block
    assert "install.sh" not in block


# ── CLI smoke ───────────────────────────────────────────────────────────────


def test_cli_setup_runs(home, tmux_yes, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO())  # non-tty
    rc = tmux_setup.main(["tmux:setup", "--mode", "agent-team"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tmux_setup:" in out
