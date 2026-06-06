#!/usr/bin/env python3
"""
Atelier PostToolUse context-budget NUDGE hook.

NATIVE LIMITATION (read this before assuming the hook does more than it does):
Claude Code CANNOT auto-fire ``/compact`` at a custom token threshold. There is
no settings knob and no hook output that triggers compaction. A PostToolUse hook
can only inject context (``hookSpecificOutput.additionalContext`` /
``systemMessage``) — it cannot read the live token count and cannot run a slash
command. Therefore this is a NUDGE + SAVE-FIRST design, NOT silent automation:
when the transcript shows context has crossed ~125k tokens, the hook injects an
instruction telling the AGENT to run ``atelier:save`` (durable session state),
then ``/compact`` with focus instructions. The agent acts on the nudge; the hook
never compacts anything itself.

Scope: only active atelier sessions. PostToolUse ``matcher`` matches the TOOL
name (e.g. "Skill"), not a specific skill name, so "while using atelier skills"
is approximated by the presence of an active atelier project under cwd
(``.ai/active_project``), mirroring ``hooks/session_open.py``. Non-atelier
sessions get no output.

Robustness contract (mirrors ``hooks/session_start.py``): read stdin, NEVER
raise, exit 0 on ANY error. A crashing hook must not break the user's session.

Install (plugins auto-register via ``hooks/hooks.json`` at plugin root):
  {"matcher": "", "hooks": [{"type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/context_budget.py"}]}
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

# Default context-fill threshold (tokens) at which we nudge. ~125k leaves
# generous headroom under typical context windows for a save + focused compact.
DEFAULT_THRESHOLD_TOKENS = 125000

#: Env override for the threshold. A valid non-negative int wins; blank/garbage/
#: negative is ignored (valid-or-ignore posture — a typo never changes behavior;
#: mirrors scripts/dispatch.py::_valid_positive_int_env and pm_dispatch's env).
THRESHOLD_ENV = "ATELIER_COMPACT_THRESHOLD_TOKENS"

#: Debounce marker: records the threshold "band" we last nudged for, so we nudge
#: ONCE per crossing instead of on every tool call. We re-arm (allow another
#: nudge) only after fill drops well below threshold — the hysteresis floor.
_NUDGE_MARKER = ".atelier-compact-nudged"

#: Fraction of threshold below which a prior nudge is reset (hysteresis). If fill
#: falls under this, we treat the next crossing as a fresh band and nudge again.
_RESET_FRACTION = 0.8


def _valid_positive_int_env(value: str | None, default: int) -> int:
    """Return int(value) iff it parses to a non-negative int, else default.

    Valid-or-ignore: a blank/garbage/negative env value is IGNORED so a typo
    never crashes the hook. Mirrors scripts/dispatch.py::_valid_positive_int_env.
    """
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (ValueError, AttributeError):
        return default
    return parsed if parsed >= 0 else default


def find_active_project(cwd: Path) -> str | None:
    """Return project_id from .ai/active_project, or None if absent/empty.

    Same scope gate as hooks/session_open.py — an atelier session is "active"
    when this file names a project.
    """
    try:
        p = cwd / ".ai" / "active_project"
        if not p.exists():
            return None
        content = p.read_text(encoding="utf-8").strip()
        return content if content else None
    except OSError:
        return None


def tally_fill(transcript_path: str | None) -> int:
    """Estimate current context fill in tokens from the transcript JSONL.

    Prefer REAL usage: scan for the LAST assistant line carrying
    ``message.usage`` and return ``input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens`` (the cumulative input side ≈ current context
    fill). If no usage is present, fall back to a char/4 heuristic over message
    text. Malformed lines are skipped. Returns 0 when nothing is readable.
    """
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    if not path.exists():
        return 0

    last_usage_fill: int | None = None
    char_count = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    # Malformed line — skip, never raise.
                    continue
                if not isinstance(obj, dict):
                    continue
                message = obj.get("message")
                if isinstance(message, dict):
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        fill = _usage_fill(usage)
                        if fill is not None:
                            last_usage_fill = fill
                    char_count += _message_chars(message)
    except OSError:
        return 0

    if last_usage_fill is not None:
        return last_usage_fill
    # Heuristic fallback: ~4 chars per token.
    return char_count // 4


def _usage_fill(usage: dict) -> int | None:
    """Sum the cumulative input-side token fields from a usage dict.

    Returns None if none of the recognized fields are present/numeric.
    """
    total = 0
    seen = False
    for key in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            total += val
            seen = True
    return total if seen else None


def _message_chars(message: dict) -> int:
    """Count characters of textual content in a transcript message (heuristic)."""
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        chars = 0
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    chars += len(text)
            elif isinstance(block, str):
                chars += len(block)
        return chars
    return 0


def _read_marker(marker_path: Path) -> int | None:
    """Read the recorded nudged-band threshold, or None if absent/unreadable."""
    try:
        if not marker_path.exists():
            return None
        raw = marker_path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def should_nudge(fill: int, threshold: int, marker_path: Path) -> bool:
    """Decide whether to emit a nudge given fill, threshold, and debounce state.

    Nudge when fill >= threshold AND we have not already nudged for this
    crossing. The debounce marker records the threshold of the band we nudged
    for. If fill has since dropped below the hysteresis floor
    (``_RESET_FRACTION * threshold``), the prior nudge is reset so a fresh
    crossing nudges again.
    """
    recorded = _read_marker(marker_path)

    if fill < threshold:
        # Below threshold. If we fell well below a prior band, re-arm.
        if recorded is not None and fill < int(threshold * _RESET_FRACTION):
            with contextlib.suppress(OSError):
                marker_path.unlink()
        return False

    # At/above threshold. Nudge unless we already nudged for this band.
    return not (recorded is not None and recorded == threshold)


def _record_nudge(marker_path: Path, threshold: int) -> None:
    """Persist the nudged band (the threshold) for debounce. Best-effort."""
    with contextlib.suppress(OSError):
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(str(threshold), encoding="utf-8")


def build_nudge_text(fill: int, threshold: int) -> str:
    """Build the imperative agent-facing nudge instruction.

    Accurate and non-injectable: the only tool-call-shaped text is the explicit
    ``atelier:save`` + ``/compact`` instruction this hook intends.
    """
    fill_k = fill // 1000
    threshold_k = threshold // 1000
    return (
        f"Atelier context-budget policy: this session's context has reached "
        f"~{fill_k}k tokens (>= the {threshold_k}k threshold). BEFORE continuing "
        f"the current task, do BOTH of the following, in order:\n"
        f"  1. Run `atelier:save` to persist session state durably (so nothing "
        f"is lost across the compaction).\n"
        f"  2. Then run `/compact` WITH focus instructions to preserve the "
        f"load-bearing details: open threads, the decisions log, the task DAG + "
        f"current wave state, branch/PR/commit state, and any abandonment "
        f"reasons.\n"
        f"This is the atelier context-budget nudge; Claude Code cannot auto-fire "
        f"/compact, so you must run the save + compact yourself now."
    )


def emit_nudge(fill: int, threshold: int) -> None:
    """Write the hook output JSON (additionalContext + systemMessage) to stdout."""
    text = build_nudge_text(fill, threshold)
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        },
        "systemMessage": (
            f"Atelier: context ~{fill // 1000}k tokens (>= {threshold // 1000}k) "
            f"— run atelier:save then /compact (see injected instruction)."
        ),
    }
    sys.stdout.write(json.dumps(payload))


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
        if find_active_project(cwd) is None:
            return 0

        threshold = _valid_positive_int_env(os.environ.get(THRESHOLD_ENV), DEFAULT_THRESHOLD_TOKENS)

        fill = tally_fill(data.get("transcript_path"))

        marker_path = cwd / ".ai" / _NUDGE_MARKER
        if should_nudge(fill, threshold, marker_path):
            emit_nudge(fill, threshold)
            _record_nudge(marker_path, threshold)
        return 0
    except Exception:
        # Per hook spec: never raise out of a hook.
        return 0


if __name__ == "__main__":
    sys.exit(main())
