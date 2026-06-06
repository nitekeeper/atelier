"""Tests for hooks/pre_compact.py — the pre-compaction snapshot safety net."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Add hooks dir to path for direct import.
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import pre_compact  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "pre_compact.py"


def _make_atelier_cwd(tmp_path: Path, project_id: str = "1") -> Path:
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir(exist_ok=True)
    (ai_dir / "active_project").write_text(project_id, encoding="utf-8")
    return tmp_path


def _write_transcript(tmp_path: Path, lines: list[dict | str]) -> Path:
    path = tmp_path / "transcript.jsonl"
    out = []
    for line in lines:
        out.append(line if isinstance(line, str) else json.dumps(line))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _msg(role: str, text: str) -> dict:
    return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _run_hook(cwd: Path, stdin_obj: dict):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}
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
# Subprocess: snapshot written for both triggers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["auto", "manual"])
def test_snapshot_written_both_triggers(tmp_path, trigger):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(
        tmp_path,
        [_msg("user", "implement feature X"), _msg("assistant", "decision: use approach Y")],
    )
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t), "trigger": trigger})
    assert result.returncode == 0
    snap_dir = cwd / ".ai" / "compact-snapshots"
    files = list(snap_dir.glob(f"*-{trigger}.md"))
    assert len(files) == 1, f"expected one snapshot for {trigger}, got {files}"
    body = files[0].read_text(encoding="utf-8")
    assert "implement feature X" in body
    assert "decision: use approach Y" in body
    assert f"trigger: {trigger}" in body


def test_does_not_block_compaction(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = _write_transcript(tmp_path, [_msg("user", "hi")])
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t), "trigger": "auto"})
    assert result.returncode == 0
    # Output, if any, must NOT contain a block decision.
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        assert "decision" not in payload
        # systemMessage is observe-only.
        assert "systemMessage" in payload


def test_non_atelier_session_silent(tmp_path):
    t = _write_transcript(tmp_path, [_msg("user", "hi")])
    result = _run_hook(
        tmp_path, {"cwd": str(tmp_path), "transcript_path": str(t), "trigger": "auto"}
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not (tmp_path / ".ai" / "compact-snapshots").exists()


def test_missing_transcript_tolerated(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    result = _run_hook(
        cwd, {"cwd": str(cwd), "transcript_path": "/no/such/file.jsonl", "trigger": "manual"}
    )
    assert result.returncode == 0
    # Snapshot still written (with empty transcript note), never crashes.
    files = list((cwd / ".ai" / "compact-snapshots").glob("*-manual.md"))
    assert len(files) == 1
    assert "no transcript content available" in files[0].read_text(encoding="utf-8")


def test_malformed_transcript_tolerated(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    t = tmp_path / "transcript.jsonl"
    t.write_text("not json\n{ bad\n", encoding="utf-8")
    result = _run_hook(cwd, {"cwd": str(cwd), "transcript_path": str(t), "trigger": "auto"})
    assert result.returncode == 0


def test_bad_stdin_exits_zero(tmp_path):
    cwd = _make_atelier_cwd(tmp_path)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Unit: determinism + builders
# ---------------------------------------------------------------------------


def test_snapshot_deterministic_given_fixed_input():
    msgs = [("user", "do thing"), ("assistant", "did thing")]
    a = pre_compact.build_snapshot("auto", "", "1", msgs, now_iso="2026-06-06T00:00:00Z")
    b = pre_compact.build_snapshot("auto", "", "1", msgs, now_iso="2026-06-06T00:00:00Z")
    assert a == b
    assert "do thing" in a
    assert "2026-06-06T00:00:00Z" in a


def test_custom_instructions_included():
    body = pre_compact.build_snapshot(
        "manual", "keep the DAG", "1", [], now_iso="2026-06-06T00:00:00Z"
    )
    assert "keep the DAG" in body


def test_collect_messages_truncates_long(tmp_path):
    long = "y" * (pre_compact._PER_MESSAGE_CAP + 500)
    t = _write_transcript(tmp_path, [_msg("user", long)])
    msgs = pre_compact.collect_messages(str(t))
    assert len(msgs) == 1
    assert "[truncated]" in msgs[0][1]


def test_collect_messages_tail_cap(tmp_path):
    lines = [_msg("user", f"m{i}") for i in range(pre_compact._TAIL_MESSAGES + 20)]
    t = _write_transcript(tmp_path, lines)
    msgs = pre_compact.collect_messages(str(t))
    assert len(msgs) == pre_compact._TAIL_MESSAGES


def test_slug_sanitizes():
    assert pre_compact._slug("auto/../etc") == "auto-etc"
    assert pre_compact._slug("") == "unknown"


class TestScopeGate:
    def test_no_active_project(self, tmp_path):
        assert pre_compact.find_active_project(tmp_path) is None

    def test_valid(self, tmp_path):
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("9")
        assert pre_compact.find_active_project(tmp_path) == "9"
