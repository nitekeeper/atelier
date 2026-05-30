"""Atelier agent-team tmux config writer — consent-gated + idempotent (atelier#63).

In agent-team mode (resolved by :func:`scripts.dispatch.resolve_dispatch_mode`)
Atelier renders the PM pane + worker panes inside a single tmux window. To make
that render legible — Prefix keybindings to focus/cycle panes, a composite
``pane-border-format`` that carries BOTH Claude Code's OSC-2 activity glyph and
Atelier's own ``@desired_title`` wave/role label, and an OPTIONAL detect-and-
source integration of the third-party ``accessd/tmux-agent-indicator`` plugin —
Atelier writes a small, marker-wrapped block of tmux directives.

Design facts (atelier design spec §9.3 / §21.6 — authoritative):

  * Atelier owns a DEDICATED file ``~/.config/atelier/tmux.conf`` and writes the
    marker-wrapped CONFIG_BLOCK there. It NEVER edits the operator's
    ``~/.tmux.conf`` beyond appending ONE ``source-file`` include line (with a
    timestamped backup taken first when the file pre-exists).
  * Keybindings are **Prefix-based** (NOT Meta): ``Prefix+p`` focuses the PM
    pane, ``Prefix+w`` cycles worker panes, ``Prefix+1..9`` jumps to worker N.
  * The marker prefix is ``# >>> atelier agent-teams v{} >>>`` — DISTINCT from
    kaizen's ``# >>> agent-teams v{} >>>`` so the two tools never match or
    clobber each other's blocks even on a machine that runs both.

Consent + safety contract (fail-safe = NO WRITE):

  * Nothing is written unless agent-team mode is active AND tmux is available
    AND the operator consents (``--yes`` or an interactive ``y``/``yes``).
  * A non-interactive stdin can NEVER consent — it returns a no-write skip.
  * Atelier NEVER installs the plugin, NEVER runs ``curl`` / ``install.sh``,
    and NEVER opens ``~/.claude/settings.json`` for write. The only writes are
    the two tmux files (+ a one-time backup of a pre-existing ``~/.tmux.conf``).

Idempotency contract: re-running at the same :data:`MARKER_VERSION` is a no-op
on BOTH steps — the config block is detected and left in place, and the include
line is detected and not re-appended (no second backup). The two steps are
INDEPENDENT: a version-current config block does NOT short-circuit the source-
line step (and vice-versa), so a half-applied prior run self-heals.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts import preflight
from scripts.dispatch import DISPATCH_MODE_AGENT_TEAM

# MARKER_VERSION is the installed-config schema version, NOT the tmux binary
# version. Bump it (and CONFIG_BLOCK in the same change) when the directives
# change so existing installs detect "an update is available".
MARKER_VERSION = 1

# Atelier-namespaced markers. The leading ``atelier `` token is what keeps these
# distinct from kaizen's ``# >>> agent-teams v{} >>>`` markers — the detector +
# stripper match ONLY the atelier prefix, so the two tools cannot collide.
MARKER_START = "# >>> atelier agent-teams v{} >>>"
MARKER_END = "# <<< atelier agent-teams v{} <<<"

# Atelier-owned dedicated config file + the operator's user tmux config.
ATELIER_TMUX_CONF = Path.home() / ".config" / "atelier" / "tmux.conf"
USER_TMUX_CONF = Path.home() / ".tmux.conf"

# The single include line Atelier appends to ``~/.tmux.conf`` and a self-
# documenting comment that precedes it so an operator reading their config knows
# who added it and how to undo it.
SOURCE_LINE = "source-file ~/.config/atelier/tmux.conf"
SOURCE_LINE_COMMENT = (
    "# added by atelier (agent-team mode) — sources the atelier-managed tmux block"
)

# The marker-prefix substrings used for detection/stripping (version-agnostic).
_MARKER_START_PREFIX = "# >>> atelier agent-teams v"
_MARKER_END_PREFIX = "# <<< atelier agent-teams v"

# ── CONFIG_BLOCK ────────────────────────────────────────────────────────────
#
# Three sections, in order:
#   1. The 3 Prefix keybindings (PM = pane 0 at left 1/3; workers = panes 1..N).
#   2. The composite pane-border-format render (FOLD-IN from kaizen #76/#79 —
#      tmux directives are byte-identical; only the COMMENTS are re-labeled to
#      atelier provenance). It composes CC's OSC-2 activity glyph
#      (``#{=1:pane_title}``) with atelier's ``@desired_title`` (immune to OSC-2;
#      ``allow-rename off`` does NOT gate OSC-2). Set UNCONDITIONALLY so it is
#      the zero-dependency FALLBACK.
#   3. The OPTIONAL detect-and-source ``if-shell -b`` guard (FOLD-IN, byte-
#      identical) for ``accessd/tmux-agent-indicator``. Re-evaluated at config
#      LOAD time; a no-op when the plugin dir is absent. Atelier NEVER installs
#      the plugin, NEVER curls install.sh, and NEVER writes
#      ``~/.claude/settings.json``; the plugin drives its 3-state indicator via
#      Claude Code hooks + ``set-option``/``set-hook`` and does NOT require
#      ``allow-passthrough``.
CONFIG_BLOCK = """# Atelier agent-team keybindings (design §9.3): p=focus PM pane, w=cycle worker panes, 1..9=jump to worker N.
# NOTE: Prefix+1..9 intentionally overrides tmux's default window-select binds inside this atelier-managed block (operator opted in via consent).
bind p select-pane -t 0
bind -r w select-pane -t :.+
bind 1 select-pane -t 1
bind 2 select-pane -t 2
bind 3 select-pane -t 3
bind 4 select-pane -t 4
bind 5 select-pane -t 5
bind 6 select-pane -t 6
bind 7 select-pane -t 7
bind 8 select-pane -t 8
bind 9 select-pane -t 9

# Pane border format — compose CC's OSC 2 activity glyph (#{=1:pane_title})
# with atelier's @desired_title (with pane_title fallback for un-tagged panes).
# Dual-signal: the leading char of pane_title carries CC's idle/busy indicator;
# @desired_title carries the wave/role label and is immune to OSC 2.
# This render is UNCONDITIONAL — the zero-dependency fallback.
set -g pane-border-status top
set -g pane-border-format '#{=1:pane_title} #[fg=cyan]#{?@desired_title,#{@desired_title},#{pane_title}}#[default]'
set -g main-pane-width 60

# OPTIONAL detect-and-source integration of the third-party
# accessd/tmux-agent-indicator plugin (3-state running/needs-input/done off
# CC hooks). Guarded by if-shell -b so detection re-runs at config LOAD time;
# a no-op when the plugin dir is absent. The plugin drives state via CC hooks +
# set-option/set-hook (see this module's docstring for the safety posture).
if-shell -b '[ -d "$HOME/.tmux/plugins/tmux-agent-indicator" ]' " \\
    source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux' ; \\
    set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖' ; \\
    set -g @agent-indicator-indicator-enabled 'on' ; \\
    set -g status-right '#{agent_indicator} | %H:%M' \\
"
"""


# ── Path helpers (resolve Path.home() at CALL time so HOME-monkeypatch works) ─


def _atelier_conf_path() -> Path:
    """Atelier's dedicated config file, resolved against the CURRENT HOME.

    Reading ``Path.home()`` at call time (rather than trusting the import-time
    module constant) lets tests monkeypatch ``Path.home`` to a tmp dir and have
    every write land under it.
    """
    return Path.home() / ".config" / "atelier" / "tmux.conf"


def _user_conf_path() -> Path:
    """The operator's ``~/.tmux.conf``, resolved against the CURRENT HOME."""
    return Path.home() / ".tmux.conf"


# ── Block assembly + detection ──────────────────────────────────────────────


def _full_block(version: int) -> str:
    """Return the marker-wrapped block at ``version`` (trailing newline)."""
    return f"{MARKER_START.format(version)}\n{CONFIG_BLOCK}{MARKER_END.format(version)}\n"


def _read_text(path: Path) -> str:
    """Read ``path`` as UTF-8, returning '' for a missing file.

    Any OS error OTHER than FileNotFound propagates so a permission problem
    surfaces loudly instead of silently masquerading as an empty file.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def detect_existing_marker(path: Path) -> int | None:
    """Scan ``path`` for an atelier agent-teams marker and return its version.

    Returns:
        * the integer version of the START marker when present and well-formed
        * ``None`` when the file is missing OR contains no atelier marker

    Raises:
        ValueError when a marker is present but malformed — a non-numeric
        version, or START/END markers that disagree in count or version.
    """
    text = _read_text(path)
    if not text:
        return None
    start_versions: list[int] = []
    end_versions: list[int] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        for prefix, bucket in (
            (_MARKER_START_PREFIX, start_versions),
            (_MARKER_END_PREFIX, end_versions),
        ):
            if line.startswith(prefix):
                suffix = line[len(prefix) :]
                num_str = suffix.split()[0] if suffix else ""
                try:
                    bucket.append(int(num_str))
                except ValueError as exc:
                    raise ValueError(
                        f"Malformed atelier agent-teams marker in {path}: "
                        f"could not parse version from line {raw_line!r}"
                    ) from exc
    if not start_versions and not end_versions:
        return None
    if not start_versions or not end_versions:
        raise ValueError(
            f"Malformed atelier agent-teams marker in {path}: "
            f"START and END counts disagree ({len(start_versions)} starts, "
            f"{len(end_versions)} ends)"
        )
    if start_versions[0] != end_versions[0]:
        raise ValueError(
            f"Malformed atelier agent-teams marker in {path}: "
            f"START version {start_versions[0]} != END version {end_versions[0]}"
        )
    return start_versions[0]


def _strip_existing_block(text: str) -> str:
    """Remove any atelier agent-teams block (and its padding blanks) from ``text``."""
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if not skipping and stripped.startswith(_MARKER_START_PREFIX):
            while out and out[-1].strip() == "":
                out.pop()
            skipping = True
            continue
        if skipping:
            if stripped.startswith(_MARKER_END_PREFIX):
                skipping = False
            continue
        out.append(line)
    joined = "\n".join(out)
    if text.endswith("\n") and not joined.endswith("\n"):
        joined += "\n"
    return joined


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file in the same dir + replace).

    A crash mid-write can never leave a half-written config: the temp file is
    fully written and fsynced-by-rename via :func:`os.replace`, which is atomic
    on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmux-conf-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        # Best-effort cleanup of the temp file if the replace never happened.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def apply_config_block(path: Path, version: int) -> None:
    """Install or replace the atelier agent-teams block in ``path`` (atomic write).

    * Missing file → create it containing only the block.
    * File present, no atelier marker → append the block with a blank-line
      separator (existing content preserved).
    * File present WITH an atelier marker → replace it in place.

    Idempotent: calling twice at the same version with no intervening edit
    leaves the file byte-identical the second time.
    """
    existing = _read_text(path)
    block = _full_block(version)
    if not existing:
        _atomic_write(path, block)
        return
    if detect_existing_marker(path) is not None:
        stripped = _strip_existing_block(existing)
        # An all-whitespace remnant (e.g. the lone trailing ``\n`` left when the
        # file contained ONLY the block) is treated as empty, so a create-then-
        # reapply at the same version is byte-identical rather than accreting
        # blank lines. Only genuine user content gets a blank-line separator.
        if not stripped.strip():
            sep = ""
            stripped = ""
        elif not stripped.endswith("\n"):
            sep = "\n\n"
        else:
            sep = "\n"
        _atomic_write(path, stripped + sep + block)
        return
    if not existing.endswith("\n"):
        sep = "\n\n"
    elif not existing.endswith("\n\n"):
        sep = "\n"
    else:
        sep = ""
    _atomic_write(path, existing + sep + block)


def show_diff(path: Path, version: int) -> str:
    """Return a lightweight human-readable preview of what apply_config_block does.

    Four modes: create / append / no-op / update. Used by the consent prompt;
    intentionally NOT a machine-parseable diff.
    """
    if not path.exists():
        return f"(create) {path} ← new file with v{version} block:\n\n{_full_block(version)}"
    existing_version = detect_existing_marker(path)
    if existing_version is None:
        return f"(append) {path}:\n\n{_full_block(version)}"
    if existing_version == version:
        return f"(no-op) {path} already at v{version}"
    return (
        f"(update) {path}: replace v{existing_version} with v{version}:\n\n{_full_block(version)}"
    )


# ── User ~/.tmux.conf include line (independent idempotent step) ─────────────


def source_line_present(user_tmux_conf: Path) -> bool:
    """True iff ``user_tmux_conf`` already includes the atelier source-file line.

    Matches the canonical ``source-file ~/.config/atelier/tmux.conf`` and also
    tolerates the ``source `` alias form, ignoring surrounding whitespace. A
    missing file is False.
    """
    text = _read_text(user_tmux_conf)
    if not text:
        return False
    target = "~/.config/atelier/tmux.conf"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (line.startswith("source-file ") or line.startswith("source ")) and target in line:
            return True
    return False


def add_source_line(user_tmux_conf: Path) -> bool:
    """Append the atelier include line to ``user_tmux_conf`` (backup-first if it exists).

    * Already present → return ``False`` (no write, no backup).
    * File exists → write a byte-identical backup ``~/.tmux.conf.bak.<iso8601-utc>``
      FIRST, then APPEND the comment + source line (existing lines untouched).
    * File absent → create it containing the comment + source line (no backup).

    Returns ``True`` when it wrote the line.
    """
    if source_line_present(user_tmux_conf):
        return False
    addition = f"{SOURCE_LINE_COMMENT}\n{SOURCE_LINE}\n"
    existing = _read_text(user_tmux_conf)
    if existing:
        # Microsecond-precise UTC stamp so two backups taken within the same
        # second can never collide and clobber each other. (The real setup_tmux
        # path can't re-enter — the first call adds the include line, so a second
        # call short-circuits via source_line_present — but a unique name keeps
        # add_source_line safe under direct/repeated use.)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        backup = user_tmux_conf.with_name(f"{user_tmux_conf.name}.bak.{stamp}")
        sep = "" if existing.endswith("\n") else "\n"
        # Atomic backup + atomic append (symmetric with apply_config_block): a
        # crash can never leave a torn backup or a half-appended ~/.tmux.conf.
        _atomic_write(backup, existing)
        _atomic_write(user_tmux_conf, existing + sep + addition)
    else:
        _atomic_write(user_tmux_conf, addition)
    return True


# ── Consent prompt ──────────────────────────────────────────────────────────


def _prompt_consent() -> bool:
    """Ask the operator to consent to the two tmux writes. Fail-safe default = NO.

    A non-interactive stdin can never consent (returns ``False`` without
    prompting). Otherwise the prompt names BOTH exact paths, the never-touch-
    ``~/.claude/settings.json`` promise, that no plugin is installed, and how to
    undo. Only an explicit ``y``/``yes`` returns ``True``; the default is **N**.
    """
    if not sys.stdin.isatty():
        return False
    atelier_conf = _atelier_conf_path()
    user_conf = _user_conf_path()
    message = (
        "Atelier agent-team mode wants to set up tmux for the PM + worker pane layout.\n"
        f"  It will WRITE:  {atelier_conf}\n"
        f"                  (an atelier-owned, marker-wrapped config block)\n"
        f"  It will APPEND one include line to: {user_conf}\n"
        f"                  (a timestamped {user_conf.name}.bak.<ts> backup is made first)\n"
        "  It will NEVER touch ~/.claude/settings.json, never run curl/install.sh,\n"
        "  and never install any tmux plugin.\n"
        "  Undo: delete the marked block from the atelier file and the source-file line\n"
        f"        from {user_conf}.\n"
        "Proceed? [y/N]: "
    )
    answer = input(message).strip().lower()
    return answer in ("y", "yes")


# ── Orchestration entry point ───────────────────────────────────────────────


def setup_tmux(*, assume_yes: bool = False, mode: str | None = None) -> dict:
    """Set up the atelier tmux config (gated, consented, idempotent).

    Returns a structured observable::

        {"config_written": bool, "source_added": bool,
         "skipped": bool, "reason": str | None}

    GATE (no-op fallback): if ``mode`` is provided and != ``"agent-team"``, OR
    tmux is unavailable, returns a no-write skip with reason
    ``"not-agent-team-or-no-tmux"``.

    CONSENT: unless ``assume_yes``, prompts. A decline OR a non-interactive
    stdin returns a no-write skip (``"declined"`` / ``"non-interactive"``).
    Fail-safe default = NO WRITE.

    On consent, runs TWO INDEPENDENT idempotent steps (a version-current config
    block does NOT short-circuit the source-line step):

      1. atelier-conf step — already at MARKER_VERSION ⇒ ``config_written=False``;
         else apply the block ⇒ ``config_written=True``.
      2. source-line step (ALWAYS evaluated) — include line present ⇒
         ``source_added=False``; else append it ⇒ ``source_added=True``.
    """
    if (mode is not None and mode != DISPATCH_MODE_AGENT_TEAM) or not preflight.tmux_available():
        return {
            "config_written": False,
            "source_added": False,
            "skipped": True,
            "reason": "not-agent-team-or-no-tmux",
        }

    if not assume_yes:
        if not sys.stdin.isatty():
            return {
                "config_written": False,
                "source_added": False,
                "skipped": True,
                "reason": "non-interactive",
            }
        if not _prompt_consent():
            return {
                "config_written": False,
                "source_added": False,
                "skipped": True,
                "reason": "declined",
            }

    atelier_conf = _atelier_conf_path()
    user_conf = _user_conf_path()

    # Step 1 — atelier config block (independent of step 2).
    if detect_existing_marker(atelier_conf) == MARKER_VERSION:
        config_written = False
    else:
        apply_config_block(atelier_conf, MARKER_VERSION)
        config_written = True

    # Step 2 — user include line (ALWAYS evaluated, regardless of step 1).
    if source_line_present(user_conf):
        source_added = False
    else:
        source_added = add_source_line(user_conf)

    skipped = (not config_written) and (not source_added)
    return {
        "config_written": config_written,
        "source_added": source_added,
        "skipped": skipped,
        "reason": None,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmux_setup",
        description=("Set up the atelier agent-team tmux config (consent-gated + idempotent)."),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    setup = sub.add_parser(
        "tmux:setup",
        help="Write the atelier tmux config block + the ~/.tmux.conf include line.",
    )
    setup.add_argument(
        "--yes",
        action="store_true",
        help="Assume consent (skip the interactive prompt).",
    )
    setup.add_argument(
        "--mode",
        default=None,
        help="Dispatch mode gate ('agent-team' to proceed; anything else no-ops).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "tmux:setup":
        result = setup_tmux(assume_yes=args.yes, mode=args.mode)
        sys.stdout.write(f"tmux_setup: {result}\n")
        return 0
    parser.error(f"unknown command {args.cmd!r}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
