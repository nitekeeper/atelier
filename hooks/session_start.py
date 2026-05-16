#!/usr/bin/env python3
"""
Atelier SessionStart hook.

Reads skills/execute/SKILL.md and prints its body (frontmatter stripped)
to stdout. Claude Code injects stdout as system context for the new session,
giving the agent the trigger contract and bypass procedure from the first
user message.

Hook spec: never block a session. On any error, print nothing and exit 0.

Install: add to .claude/settings.json:
  {
    "hooks": {
      "SessionStart": [
        {"matcher": "", "hooks": [
          {"type": "command",
           "command": "python /path/to/atelier/hooks/session_start.py"}
        ]}
      ]
    }
  }
"""
import re
import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_SKILL_PATH = _HOOK_DIR.parent / "skills" / "execute" / "SKILL.md"


def main() -> int:
    try:
        if not _SKILL_PATH.exists():
            # Canonical file missing -- silently exit 0 (never block a session).
            return 0
        text = _SKILL_PATH.read_text(encoding="utf-8")
        # Strip YAML frontmatter if present (--- delimited block at file start)
        match = re.match(r"^---\r?\n.*?\r?\n---\r?\n(.*)$", text, re.DOTALL)
        body = match.group(1) if match else text
        # Write as UTF-8 bytes to avoid codec issues on Windows (cp1252 default).
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write(body.encode("utf-8"))
        else:
            sys.stdout.write(body)  # fallback when stdout is replaced (e.g., StringIO in tests)
        return 0
    except Exception:
        # Per hook spec: never raise out of a hook.
        return 0


if __name__ == "__main__":
    sys.exit(main())
