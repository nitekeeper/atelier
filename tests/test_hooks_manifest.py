"""Validates hooks/hooks.json — plugin hook auto-registration manifest."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"


def test_hooks_json_is_valid_json():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "hooks" in data


def test_registers_both_hooks_via_plugin_root():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "PostToolUse" in hooks
    assert "PreCompact" in hooks

    commands = []
    for event in ("PostToolUse", "PreCompact"):
        for entry in hooks[event]:
            assert entry.get("matcher") == ""
            for h in entry["hooks"]:
                assert h["type"] == "command"
                commands.append(h["command"])

    joined = "\n".join(commands)
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/context_budget.py" in joined
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py" in joined


def test_referenced_scripts_exist():
    assert (REPO_ROOT / "hooks" / "context_budget.py").exists()
    assert (REPO_ROOT / "hooks" / "pre_compact.py").exists()
