#!/usr/bin/env python3
"""
Atelier session open hook.
Reads the latest session from the DB and announces project phase to Claude context.

Install as a PreToolUse hook in .claude/settings.json. Example:
  {
    "hooks": {
      "PreToolUse": [
        {"matcher": "", "hooks": [
          {"type": "command",
           "command": "python /path/to/atelier/hooks/session_open.py"}
        ]}
      ]
    }
  }

Option B (from spec): DB errors never block a session. Errors produce a warning
and Claude continues with reduced context.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


# Path to session.py relative to this hook file.
# hooks/ is at atelier-root/hooks/; scripts/ is at atelier-root/scripts/.
_HOOK_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HOOK_DIR.parent / "scripts"
_USING_ATELIER_PATH = _HOOK_DIR.parent / "skills" / "using-atelier" / "SKILL.md"

# Flag to prevent announcing more than once per session.
_FLAG_NAME = ".atelier-session-announced"


def find_active_project(cwd: Path) -> str | None:
    """Return project_id from .ai/active_project, or None if absent/empty."""
    p = cwd / ".ai" / "active_project"
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8").strip()
    return content if content else None


def fetch_latest_session(scripts_dir: Path, project_id: str) -> dict | None | str:
    """Call session.py read-latest <project_id>.

    Returns:
        dict  — parsed session row (session found)
        None  — no prior session for this project (session.py returned 0 + empty)
        str   — error message starting with "error:" (any failure)
    """
    try:
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "session.py"), "read-latest", project_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except Exception as exc:
        return f"error:{exc}"

    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown error"
        return f"error:{stderr}"

    output = result.stdout.strip()
    if not output:
        return None  # No prior session

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return "error:invalid JSON from session.py"


def build_announcement(project_id: str, session: dict | None) -> str:
    """Build the context announcement string from session data."""
    if session is None:
        return f"Atelier: Project {project_id} — no prior session recorded."

    phase = session.get("phase", "unknown")
    parts = [f"Atelier: Project {project_id} — resuming at {phase}."]

    if session.get("pm_notes"):
        parts.append(f"Notes: {session['pm_notes']}")
    if session.get("next_action"):
        parts.append(f"Next action: {session['next_action']}")
    if session.get("status") == "blocked" and session.get("blocking_reason"):
        parts.append(f"BLOCKED: {session['blocking_reason']}")

    return " ".join(parts)


def get_phase_guidance(phase: str) -> str | None:
    """Read the phase guidance table from using-atelier/SKILL.md and return the
    line for `phase`, formatted for hook output. Returns None on any failure
    (missing file, table not found, phase not present) -- caller should not
    block the session on a None return."""
    try:
        if not _USING_ATELIER_PATH.exists():
            return None
        text = _USING_ATELIER_PATH.read_text(encoding="utf-8")
        # CRLF-tolerant section match
        section = re.search(r"## Phase guidance\r?\n(.*?)(?=\r?\n## )", text, re.DOTALL)
        if not section:
            return None
        # Each table row: | `phase` | recommendation | `skill` |
        row_pattern = re.compile(
            rf"\|\s*`{re.escape(phase)}`\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|",
        )
        match = row_pattern.search(section.group(1))
        if not match:
            return None
        recommendation = match.group(1).strip()
        skill = match.group(2).strip()
        return f"Recommended next action: {recommendation} ({skill})"
    except Exception:
        return None


def main() -> None:
    cwd = Path.cwd()
    flag = cwd / _FLAG_NAME

    # Only announce once per session.
    if flag.exists():
        sys.exit(0)

    project_id = find_active_project(cwd)
    if not project_id:
        # No active project configured — silent exit.
        sys.exit(0)

    result = fetch_latest_session(_SCRIPTS_DIR, project_id)

    if isinstance(result, str) and result.startswith("error:"):
        # Option B: warn and continue. Do not block Claude.
        msg = result[6:]
        print(
            f"Atelier: warning — could not read session ({msg}). "
            "Continuing without session context.",
            flush=True,
        )
    else:
        announcement = build_announcement(project_id, result)
        print(announcement, flush=True)
        current_phase = result.get("phase") if isinstance(result, dict) else None
        if current_phase:
            guidance = get_phase_guidance(current_phase)
            if guidance:
                print(guidance, flush=True)

    # Mark announced — suppress further invocations this session.
    try:
        flag.write_text("announced", encoding="utf-8")
    except OSError:
        pass  # Non-fatal


if __name__ == "__main__":
    main()
