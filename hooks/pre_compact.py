#!/usr/bin/env python3
"""
Atelier PreCompact SAVE safety-net hook.

NATIVE LIMITATION (read before assuming this hook steers the summary): the
PreCompact hook fires for BOTH ``auto`` and ``manual`` compaction triggers, but
it CANNOT steer the compaction summary and CANNOT read token counts. It can only
block (which we never do for normal compaction) or observe. Crucially, important
session detail can be lost in compaction and the hook cannot prevent that at the
summary level. So this hook's job is a durable, hook-level SAFETY NET: it writes
a deterministic, timestamped snapshot of the recent transcript to a sidecar under
``.ai/compact-snapshots/`` BEFORE compaction proceeds. Nothing in the transcript
is lost even though the summary itself is out of our control.

This is distinct from the agent-level durable save: hooks CANNOT invoke skills,
so this hook does NOT call ``atelier:save``. The agent-level ``atelier:save`` is
driven by the PostToolUse nudge in ``hooks/context_budget.py``. The snapshot file
IS the hook-level net.

Scope: only active atelier sessions (``.ai/active_project`` present under cwd,
mirroring ``hooks/session_open.py``). Non-atelier sessions exit 0 silently.

Robustness contract (mirrors ``hooks/session_start.py``): read stdin, NEVER
raise, exit 0 on ANY error. PreCompact must NOT block normal compaction — it
emits no ``decision`` and exits 0.

Install (plugins auto-register via ``hooks/hooks.json`` at plugin root):
  {"matcher": "", "hooks": [{"type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py"}]}
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Directory (under .ai/) where pre-compaction snapshots are written.
_SNAPSHOT_DIR = "compact-snapshots"

#: One-line audit log of snapshots written.
_AUDIT_LOG = ".atelier-compact-snapshots.log"

#: How many of the most recent text-bearing transcript messages to capture.
_TAIL_MESSAGES = 60

#: Per-message character cap in the snapshot (keeps the sidecar bounded).
_PER_MESSAGE_CAP = 4000


def find_active_project(cwd: Path) -> str | None:
    """Return project_id from .ai/active_project, or None if absent/empty.

    Same scope gate as hooks/session_open.py.
    """
    try:
        p = cwd / ".ai" / "active_project"
        if not p.exists():
            return None
        content = p.read_text(encoding="utf-8").strip()
        return content if content else None
    except OSError:
        return None


def _extract_text(message: dict) -> str:
    """Extract plain text from a transcript message's content."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def collect_messages(transcript_path: str | None) -> list[tuple[str, str]]:
    """Return a list of (role, text) for the tail of text-bearing messages.

    Captures user + assistant text. Malformed lines are skipped. Returns an
    empty list when nothing is readable.
    """
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.exists():
        return []

    collected: list[tuple[str, str]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role") or obj.get("type") or "unknown"
                text = _extract_text(message).strip()
                if not text:
                    continue
                if len(text) > _PER_MESSAGE_CAP:
                    text = text[:_PER_MESSAGE_CAP] + "\n…[truncated]"
                collected.append((str(role), text))
    except OSError:
        return []

    return collected[-_TAIL_MESSAGES:]


def build_snapshot(
    trigger: str,
    custom_instructions: str,
    project_id: str,
    messages: list[tuple[str, str]],
    *,
    now_iso: str,
) -> str:
    """Build the deterministic snapshot body.

    Deterministic given fixed inputs (the timestamp is supplied by the caller),
    dependency-free, plain markdown.
    """
    lines = [
        "# Atelier pre-compaction snapshot",
        "",
        f"- captured_at: {now_iso}",
        f"- trigger: {trigger}",
        f"- project_id: {project_id}",
        f"- messages_captured: {len(messages)}",
        "",
    ]
    if custom_instructions:
        lines += ["## Custom compaction instructions", "", custom_instructions, ""]
    lines += ["## Recent transcript (tail)", ""]
    if messages:
        for role, text in messages:
            lines.append(f"### {role}")
            lines.append("")
            lines.append(text)
            lines.append("")
    else:
        lines.append("_(no transcript content available)_")
        lines.append("")
    return "\n".join(lines)


def _slug(value: str) -> str:
    """Filesystem-safe slug for the trigger token."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return cleaned or "unknown"


def write_snapshot(cwd: Path, trigger: str, body: str, *, now_iso: str) -> Path | None:
    """Write the snapshot to .ai/compact-snapshots/. Returns path or None on error.

    Filename includes the trigger and a filesystem-safe timestamp so concurrent
    triggers do not collide.
    """
    try:
        snap_dir = cwd / ".ai" / _SNAPSHOT_DIR
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = re.sub(r"[^0-9]", "", now_iso)
        fname = f"{ts}-{_slug(trigger)}.md"
        out = snap_dir / fname
        out.write_text(body, encoding="utf-8")
        return out
    except OSError:
        return None


def _append_audit(cwd: Path, snapshot_path: Path, *, now_iso: str) -> None:
    """Append a one-line audit entry. Best-effort, never raises."""
    try:
        log = cwd / ".ai" / _AUDIT_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso}\t{snapshot_path}\n")
    except OSError:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0

    try:
        cwd_raw = data.get("cwd")
        cwd = Path(cwd_raw) if cwd_raw else Path.cwd()

        # Scope gate: only act for an active atelier session.
        project_id = find_active_project(cwd)
        if project_id is None:
            return 0

        trigger = str(data.get("trigger") or "unknown")
        custom_instructions = data.get("custom_instructions") or ""
        if not isinstance(custom_instructions, str):
            custom_instructions = ""

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        messages = collect_messages(data.get("transcript_path"))
        body = build_snapshot(
            trigger,
            custom_instructions,
            project_id,
            messages,
            now_iso=now_iso,
        )
        snapshot_path = write_snapshot(cwd, trigger, body, now_iso=now_iso)
        if snapshot_path is not None:
            _append_audit(cwd, snapshot_path, now_iso=now_iso)
            # Observe-only systemMessage; NO decision (never blocks compaction).
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": (
                            f"Atelier: pre-compaction snapshot written to {snapshot_path}"
                        )
                    }
                )
            )
        return 0
    except Exception:
        # Per hook spec: never raise out of a hook; never block compaction.
        return 0


if __name__ == "__main__":
    sys.exit(main())
