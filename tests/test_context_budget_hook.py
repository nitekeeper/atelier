"""Tests for hooks/context_budget.py — the 125k context-budget nudge."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Add hooks dir to path for direct import.
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import context_budget  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "context_budget.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_atelier_cwd(tmp_path: Path, project_id: str = "1") -> Path:
    """Fabricate a cwd with an active atelier project (.ai/active_project)."""
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir(exist_ok=True)
    (ai_dir / "active_project").write_text(project_id, encoding="utf-8")
    return tmp_path


def _write_transcript(tmp_path: Path, lines: list[dict | str]) -> Path:
    """Write a JSONL transcript; dict entries are json-encoded, str passed raw."""
    path = tmp_path / "transcript.jsonl"
    out = []
    for line in lines:
        out.append(line if isinstance(line, str) else json.dumps(line))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _usage_line(input_tokens=0, cache_read=0, cache_creation=0, output=0):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": output,
            },
            "content": [{"type": "text", "text": "hi"}],
        },
    }


def _run_hook(cwd: Path, stdin_obj: dict, env_extra: dict | None = None):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(stdin_obj),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
    )


# ---------------------------------------------------------------------------
# Unit: tally_fill
# ---------------------------------------------------------------------------


class TestTallyFill:
    def test_real_usage_last_line_wins(self, tmp_path):
        t = _write_transcript(
            tmp_path,
            [
                _usage_line(input_tokens=10, cache_read=5),
                _usage_line(input_tokens=100000, cache_read=20000, cache_creation=5000),
            ],
        )
        assert context_budget.tally_fill(str(t)) == 125000

    def test_char_heuristic_fallback(self, tmp_path):
        # No usage anywhere → char/4 heuristic.
        text = "x" * 400
        t = _write_transcript(
            tmp_path,
            [{"message": {"role": "user", "content": text}}],
        )
        assert context_budget.tally_fill(str(t)) == 100

    def test_malformed_line_skipped(self, tmp_path):
        t = _write_transcript(
            tmp_path,
            [
                "{ not valid json",
                _usage_line(input_tokens=130000),
            ],
        )
        assert context_budget.tally_fill(str(t)) == 130000

    def test_missing_transcript_returns_zero(self):
        assert context_budget.tally_fill("/no/such/file.jsonl") == 0
        assert context_budget.tally_fill(None) == 0


# ---------------------------------------------------------------------------
# Unit: should_nudge debounce / hysteresis
# ---------------------------------------------------------------------------


class TestShouldNudge:
    def test_below_threshold_no_nudge(self, tmp_path):
        marker = tmp_path / ".ai" / context_budget._NUDGE_MARKER
        assert context_budget.should_nudge(100000, 125000, marker) is False

    def test_at_threshold_nudges_first_time(self, tmp_path):
        marker = tmp_path / ".ai" / context_budget._NUDGE_MARKER
        assert context_budget.should_nudge(125000, 125000, marker) is True

    def test_debounce_same_band_no_repeat(self, tmp_path):
        marker = tmp_path / ".ai" / context_budget._NUDGE_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("125000")
        assert context_budget.should_nudge(130000, 125000, marker) is False

    def test_rearm_after_drop_below_floor(self, tmp_path):
        marker = tmp_path / ".ai" / context_budget._NUDGE_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text("125000")
        # Drop well below floor (0.8 * 125000 = 100000) → marker cleared.
        assert context_budget.should_nudge(50000, 125000, marker) is False
        assert not marker.exists()
        # Re-cross → nudges again.
        assert context_budget.should_nudge(125000, 125000, marker) is True


# ---------------------------------------------------------------------------
# Unit: env override
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_valid_override(self):
        assert context_budget._valid_positive_int_env("90000", 125000) == 90000

    def test_garbage_ignored(self):
        assert context_budget._valid_positive_int_env("not-a-number", 125000) == 125000

    def test_negative_ignored(self):
        assert context_budget._valid_positive_int_env("-5", 125000) == 125000

    def test_none_default(self):
        assert context_budget._valid_positive_int_env(None, 125000) == 125000


# ---------------------------------------------------------------------------
# Subprocess integration
# ---------------------------------------------------------------------------


def test_below_threshold_no_output(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(tmp_path, [_usage_line(input_tokens=1000)])
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_at_threshold_emits_nudge(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(tmp_path, [_usage_line(input_tokens=130000)])
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    ac = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "atelier:save" in ac
    assert "/compact" in ac
    assert "systemMessage" in payload
    # Debounce marker written.
    assert (cwd / ".ai" / context_budget._NUDGE_MARKER).exists()


def test_debounce_second_call_silent(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(tmp_path, [_usage_line(input_tokens=130000)])
    first = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    second = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert second.returncode == 0
    assert second.stdout.strip() == ""


def test_non_atelier_session_silent(tmp_path):
    # No .ai/active_project → not an atelier session.
    t = _write_transcript(tmp_path, [_usage_line(input_tokens=130000)])
    result = _run_hook(tmp_path, {"cwd": str(tmp_path), "transcript_path": str(t)})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_env_override_lowers_threshold(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(tmp_path, [_usage_line(input_tokens=90000)])
    # Default 125k → no nudge; override to 80k → nudge.
    base = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert base.stdout.strip() == ""
    over = _run_hook(
        cwd,
        {"cwd": str(cwd), "transcript_path": str(t)},
        env_extra={"ATELIER_COMPACT_THRESHOLD_TOKENS": "80000"},
    )
    payload = json.loads(over.stdout)
    assert "atelier:save" in payload["hookSpecificOutput"]["additionalContext"]


def test_char_heuristic_path_subprocess(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    # No usage; 600000 chars / 4 = 150000 tokens > threshold.
    t = _write_transcript(
        tmp_path,
        [{"message": {"role": "user", "content": "x" * 600000}}],
    )
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert result.returncode == 0
    assert "atelier:save" in result.stdout


def test_malformed_transcript_tolerated(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = tmp_path / "transcript.jsonl"
    t.write_text("garbage not json\n{ also bad\n", encoding="utf-8")
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t)})
    assert result.returncode == 0
    # No usable fill → below threshold → silent.
    assert result.stdout.strip() == ""


def test_bad_stdin_exits_zero(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="this is not json at all",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_empty_stdin_exits_zero(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# find_active_project scope gate
# ---------------------------------------------------------------------------


class TestScopeGate:
    def test_no_ai_dir(self, tmp_path):
        assert context_budget.find_active_project(tmp_path) is None

    def test_empty_file(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("")
        assert context_budget.find_active_project(tmp_path) is None

    def test_valid_id(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("42\n")
        assert context_budget.find_active_project(tmp_path) == "42"


def test_does_not_fake_auto_compaction():
    """Guard: the nudge output must NOT contain a fictitious auto-compact trigger."""
    text = context_budget.build_nudge_text(130000, 125000)
    assert '"compact"' not in text
    assert "compact: true" not in text.lower()
    # It must instruct the agent to run the commands, not claim to do it itself.
    assert "atelier:save" in text
    assert "/compact" in text


@pytest.mark.parametrize(
    "fill,threshold,expect", [(130000, 125000, "130k"), (200000, 125000, "200k")]
)
def test_nudge_reports_fill(fill, threshold, expect):
    assert expect in context_budget.build_nudge_text(fill, threshold)
