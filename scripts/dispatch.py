# scripts/dispatch.py
"""Atelier team-mode worker dispatch — briefing composition & contract checks.

This module is the wave-4 deliverable from epic #37 (design
``docs/specs/2026-05-25-atelier-team-mode-design.md`` §16.3 + the
``internal/team-mode-rules/SKILL.md`` rules surface). It is the
**source-of-truth** for:

* The Jinja2 :class:`~jinja2.Environment` that renders every worker
  briefing — ``StrictUndefined`` + ``autoescape=False`` (we are emitting
  a plaintext LLM prompt; the ``untrusted`` macro escapes the sender
  attribute explicitly so an HTML-meaningful ``role_id`` cannot break out
  of the fence). Trim/lstrip blocks + ``keep_trailing_newline`` match the
  contract pinned by ``tests/test_dispatch_templates.py::_make_env``.

* The :data:`REQUIRED_VARS` set declaring every name the role briefing
  pulls from its render context. ``tests/test_dispatch_templates.py``
  imports this verbatim so any drift between template + dispatch fails
  CI rather than silently rendering an empty hole. The pre-render
  validator (:func:`validate_render_context`) walks this set and raises
  :class:`MissingRenderVarsError` BEFORE Jinja2 ever opens its
  ``UndefinedError`` path, so the diagnostic surface for missing context
  is actionable (named missing vars) rather than generic.

* :func:`sanitize_bridge_field` — strips C0 control chars
  (``\x00-\x08``, ``\x0b-\x1f``) from any bridge payload before it lands
  in the render context. ``_base.j2``'s header comment names this
  function as the contract — template authors assume payloads are
  already control-char-clean.

* :func:`compose_briefing` — assembles a worker's inaugural spawn prompt
  in the fixed §16.3 order: atelier-rules header (read from
  ``internal/team-mode-rules/SKILL.md`` at composition time) → persona
  profile → phase procedure → task block → output requirements → bridge
  wiring → self-verify protocol. The structural blocks live in the
  Jinja2 template; this composer assembles the prefix (rules + persona +
  phase procedure) and feeds the merged narrative into the template's
  ``task_brief`` slot. There are NO token caps on the assembled
  briefing — they were removed in rules SKILL v1.1 because token usage
  is task-dependent and not meaningfully cappable as a static constant.
  The per-message bridge payload **byte** cap (8192 bytes) is still
  enforced by ``scripts/bridge_send.py``; that is a physical-storage
  limit on the wire, not a token budget on the prompt.

* :class:`WaveTracker` — minimal in-process bookkeeping of which
  expected participants have reported a terminal envelope status
  (``done|blocked|abandoned|needs-input``) for a given wave id.
  Foundational scaffolding only — full scheduler integration is wave-5.

* :func:`read_heartbeats` — read-only surface over the bridge log's
  ``kind='heartbeat'`` rows, returning ``(team_id, role_id,
  last_seen_iso)`` tuples. v1 treats heartbeats as informational
  liveness; the wall-clock cap is the binding stall trigger per the
  rules SKILL.md heartbeat clause.

CLI surface (stable):

    python3 scripts/dispatch.py compose
        --role <role_id> --task-id <id>
        --task-brief <text|@path>
        [--persona <text|@path>]
        [--phase-procedure <text|@path>]
        [--team <team_id>] [--team-lead <name>]
        [--wave <wave_id>] [--wave-phase <phase>]
        [--deadline <iso8601>]
        [--out -]

Stdout: a single JSON object (one line, trailing newline) — matches
``scripts/agents.py`` and ``scripts/bridge_send.py`` style — with shape:

    {"role_id": "...", "task_id": "...", "briefing": "...",
     "schema_version": 1}

The ``briefing`` field is the full rendered worker spawn prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

# ── Constants pinned to the rules SKILL + 003 migration ────────────────────

# Triple-pinned with:
#   * migrations/shared/003_team_mode.sql (PRAGMA user_version = 1)
#   * scripts/bridge_send.py::SCHEMA_VERSION
#   * scripts/bridge_read.py::SCHEMA_VERSION
#   * internal/team-mode-rules/SKILL.md frontmatter schema_version
# Bump requires a CHANGELOG entry + a new migration + bumping in lockstep.
SCHEMA_VERSION = 1

# Repo-relative location of the Jinja2 templates and the rules SKILL surface.
# Resolved against the repo root inferred from this file's path (scripts/ is a
# sibling of internal/) so the module works regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "internal" / "team-mode-templates"
RULES_SKILL = REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md"
ROLE_TEMPLATE = "briefings/role.j2"

# Control-char sweep — C0 minus TAB (\x09), LF (\x0a), CR (\x0d). Bridge
# payloads MUST be stripped of these before they reach the template per the
# comment block at the top of internal/team-mode-templates/_base.j2.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Canonical list of every variable the role briefing pulls from its outer
# render context. Mirrors `internal/team-mode-templates/_base.j2`'s
# "Required render-context vars" header. The companion test
# `tests/test_dispatch_templates.py::test_required_vars_dict_matches_template_refs`
# walks the Jinja AST to verify this set is exactly the union of refs across
# role.j2 + _base.j2 (modulo the in-template `untrusted` macro), so any drift
# fails CI rather than rendering a silent empty hole.
REQUIRED_VARS: frozenset[str] = frozenset(
    {
        "role_id",
        "task_id",
        "team_lead_name",
        "from_agent_self",
        "schema_version",
        "team_id",
        "bridge_cmds",
        "idempotency_seed",
        "wave_id",
        "wave_phase",
        "deadline_iso",
        "peers",
        "quorum_rule",
        "forbidden_actions",
        "task_brief",
        "acceptance_criteria",
    }
)

# Terminal closure tokens from TM-006. The wave tracker uses this set to
# decide whether a member's last status counts as "reported"; the two
# terminal-only tokens are `done` and `abandoned`.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "blocked", "abandoned", "needs-input"})
TERMINAL_ONLY_STATUSES: frozenset[str] = frozenset({"done", "abandoned"})


# ── Exceptions ─────────────────────────────────────────────────────────────


class DispatchError(RuntimeError):
    """Base class for explicit dispatch failures. Subclasses carry actionable
    messages so the operator (or the calling skill) can fix at source rather
    than chase a generic stack trace."""


class MissingRenderVarsError(ValueError):
    """Raised by :func:`validate_render_context` when one or more
    ``REQUIRED_VARS`` are absent (or ``None``) in the supplied context.

    Carries the sorted list of missing names so dispatch's diagnostic
    surfaces actionable info rather than Jinja2's first-undefined message.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing: list[str] = sorted(set(missing))
        msg = (
            f"render context missing {len(self.missing)} required var(s): {', '.join(self.missing)}"
        )
        super().__init__(msg)


# ── Jinja2 environment factory ────────────────────────────────────────────


def make_template_env(template_dir: Path | str = TEMPLATE_DIR) -> Environment:
    """Construct the dispatch Jinja2 environment.

    Settings are pinned by contract (mirrored in
    ``tests/test_dispatch_templates.py::_make_env``):

    * ``undefined=StrictUndefined`` — missing render vars raise
      ``UndefinedError`` rather than silently render empty.
    * ``autoescape=False`` — output is a plaintext LLM prompt. The
      ``untrusted(payload, sender)`` macro in ``_base.j2`` escapes the
      sender attribute explicitly via ``|e``.
    * ``trim_blocks=True`` + ``lstrip_blocks=True`` — strip the trailing
      newline + leading whitespace from block tags so the rendered output
      reads as a clean Markdown briefing.
    * ``keep_trailing_newline=True`` — preserve the file-final newline so
      downstream concatenation does not produce ``EOF\\n#header`` joins.
    """
    return Environment(  # nosec B701 — autoescape=False is intentional: rendered output is plaintext for an LLM, not HTML. The `untrusted` macro in _base.j2 HTML-escapes both sender and payload via `|e` per TM-008 (prompt-injection defense). Enabling autoescape would corrupt the briefing's plaintext format without adding safety.
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


# ── Pre-render context validation ─────────────────────────────────────────


def validate_render_context(ctx: Mapping[str, Any]) -> None:
    """Verify every ``REQUIRED_VARS`` name is present (and not ``None``) in
    ``ctx`` BEFORE Jinja2 begins rendering.

    Raises :class:`MissingRenderVarsError` listing every missing name.
    ``None``-valued keys are treated as missing — the template's
    ``StrictUndefined`` would render ``None`` as the string ``"None"``
    (the value IS defined, just nullish), which would produce a confusing
    briefing rather than a clear failure. We catch that here.
    """
    missing = [name for name in REQUIRED_VARS if ctx.get(name) is None]
    if missing:
        raise MissingRenderVarsError(missing)


# ── Bridge field sanitation ───────────────────────────────────────────────


def sanitize_bridge_field(value: str) -> str:
    """Strip C0 control chars (``\\x00-\\x08``, ``\\x0b-\\x1f``) from a
    bridge payload string before it lands in the render context.

    TAB (``\\x09``), LF (``\\x0a``), and CR (``\\x0d``) are preserved — they
    are legitimate prompt content (Markdown indentation, line breaks).
    Everything else in the C0 range is removed. Non-string input raises
    :class:`TypeError` so callers can't accidentally pass bytes or None.

    Matches the "Control-char stripping" contract documented in the
    ``internal/team-mode-templates/_base.j2`` preamble comment.
    """
    if not isinstance(value, str):
        raise TypeError(f"sanitize_bridge_field expects str, got {type(value).__name__}")
    return _CONTROL_CHAR_RE.sub("", value)


# ── Briefing composition ──────────────────────────────────────────────────


def _read_rules_block() -> str:
    """Read the team-mode rules SKILL.md verbatim. Per TM-007 the rendered
    rules block is prepended to every worker briefing — a stale rules
    surface MUST NOT silently propagate into worker prompts. The schema
    version pin is enforced by the bridge writer/reader at runtime; here we
    just guarantee the rules text is current with the file on disk."""
    try:
        return RULES_SKILL.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DispatchError(
            f"team-mode rules SKILL.md not found at {RULES_SKILL}; cannot "
            "compose a briefing without the rules block (TM-007)"
        ) from exc


def _default_bridge_cmds(team_id: str, role_id: str, last_seq: int = 0) -> dict[str, Any]:
    """Construct the default ``bridge_cmds`` dict the template renders into
    the CHANNELS block. Provided as a convenience so CLI callers without a
    pre-built mapping can still produce a coherent briefing — production
    dispatch (wave-5 scheduler) will pass a richer dict that may include
    last_seq advanced from the delivery cursor."""
    return {
        "send_to_lead": (
            f"python3 scripts/bridge_send.py --team {team_id} "
            f"--to <team_lead_role_id> --from {role_id} --payload @reply.json"
        ),
        "send_to_peer": (
            f"python3 scripts/bridge_send.py --team {team_id} "
            f"--to <peer_role_id> --from {role_id} --payload @msg.json"
        ),
        "read_since": (
            f"python3 scripts/bridge_read.py --team {team_id} --as {role_id} --since-seq <last_seq>"
        ),
        "heartbeat": (
            f"python3 scripts/bridge_send.py --team {team_id} --from {role_id} --kind heartbeat"
        ),
        "last_seq": last_seq,
    }


def compose_briefing(
    *,
    role_id: str,
    task_id: str | int,
    persona_profile_text: str,
    phase_procedure_text: str,
    task_brief: str,
    team_id: str,
    team_lead_name: str,
    wave_id: str,
    wave_phase: str,
    deadline_iso: str,
    peers: list[Mapping[str, Any]] | None = None,
    quorum_rule: str = "All wave teammates report `done` before next wave dispatches.",
    forbidden_actions: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    bridge_cmds: Mapping[str, Any] | None = None,
    idempotency_seed: str | None = None,
    from_agent_self: str | None = None,
    template_env: Environment | None = None,
) -> str:
    """Assemble + render a worker's inaugural spawn prompt.

    Composition order is fixed (§16.3 of the design doc + the rules SKILL):

      1. atelier-rules header (read from
         ``internal/team-mode-rules/SKILL.md`` at call time — TM-007 means
         it MUST be the current on-disk version)
      2. persona profile (the worker's identity body)
      3. phase procedure (the dev-arc step the worker is in)
      4. task block + output requirements + bridge wiring + self-verify
         protocol (all delivered by the Jinja2 template's structural
         blocks)

    The Jinja2 template owns the structural blocks for IDENTITY,
    CHANNELS, REPLY CONTRACT, WAVE CONTEXT, TASK, and ABANDON GRAMMAR;
    this composer feeds the merged rules + persona + phase-procedure
    prefix into the template's ``task_brief`` slot so the worker reads
    the full briefing as a single coherent narrative. The persona block
    is NOT in role.j2 — it enters here via the prefix join.

    There are NO token caps applied here — rules SKILL v1.1 removed
    them. The composer always returns the assembled string regardless
    of length. The only physical limit downstream is the 8 KiB
    per-bridge-message byte cap enforced by ``scripts/bridge_send.py``,
    which is unrelated to inaugural-prompt size.

    Returns the fully-rendered briefing string.
    """
    if template_env is None:
        template_env = make_template_env()

    rules_text = _read_rules_block()
    sanitized_task = sanitize_bridge_field(task_brief)

    # Compose the task_brief slot: the rules header + persona + phase
    # procedure + the actual task text. Each block is preceded by a fenced
    # heading so the worker reads the briefing as a single coherent doc
    # rather than four blind concatenated chunks.
    prefix_parts = [
        "# ATELIER TEAM-MODE RULES (verbatim — read first)",
        "",
        rules_text.rstrip(),
        "",
        "# YOUR PERSONA",
        "",
        persona_profile_text.rstrip(),
        "",
        "# PHASE PROCEDURE",
        "",
        phase_procedure_text.rstrip(),
        "",
        "# TASK (delivered by team-lead)",
        "",
        sanitized_task.rstrip(),
    ]
    composed_task_brief = "\n".join(prefix_parts)

    ctx: dict[str, Any] = {
        "role_id": role_id,
        "task_id": task_id,
        "team_lead_name": team_lead_name,
        "from_agent_self": from_agent_self or role_id,
        "schema_version": SCHEMA_VERSION,
        "team_id": team_id,
        "bridge_cmds": bridge_cmds or _default_bridge_cmds(team_id, role_id),
        "idempotency_seed": idempotency_seed or f"{team_id}:{role_id}:{task_id}",
        "wave_id": wave_id,
        "wave_phase": wave_phase,
        "deadline_iso": deadline_iso,
        "peers": list(peers or []),
        "quorum_rule": quorum_rule,
        "forbidden_actions": list(forbidden_actions or []),
        "task_brief": composed_task_brief,
        "acceptance_criteria": list(acceptance_criteria or []),
    }

    validate_render_context(ctx)

    tmpl = template_env.get_template(ROLE_TEMPLATE)
    rendered = tmpl.render(**ctx)
    return rendered


# ── Wave tracking + heartbeat monitoring ──────────────────────────────────


@dataclass
class WaveTracker:
    """In-process tracker for which expected participants of a wave have
    reported a terminal envelope status.

    Foundational scaffolding only — the wave-5 scheduler will replace
    this with a DB-backed implementation reading from the durable
    backend's task_results table. For now this is what dispatch.py
    exposes so wave-aware callers (the upcoming team-lead orchestration
    skill) can compose against a stable surface.

    Usage:

        tracker = WaveTracker(wave_id="wave-3", expected={"be-1", "sdet-1"})
        tracker.record("be-1", "done")
        tracker.record("sdet-1", "blocked")
        tracker.is_complete()      # → True iff every expected member has reported
        tracker.outstanding()      # → set of expected members who have not reported
        tracker.terminal_only()    # → True iff every reported status is done|abandoned
    """

    wave_id: str
    expected: set[str]
    reports: dict[str, str] = field(default_factory=dict)

    def record(self, role_id: str, status: str) -> None:
        """Record a member's terminal envelope status. Raises
        :class:`ValueError` if ``status`` is not one of the four TM-006
        closure tokens — silent acceptance of a typo would mask a
        contract violation."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"unknown wave status {status!r}; expected one of {sorted(TERMINAL_STATUSES)}"
            )
        self.reports[role_id] = status

    def outstanding(self) -> set[str]:
        """Members of ``expected`` who have not yet reported any status."""
        return set(self.expected) - set(self.reports)

    def is_complete(self) -> bool:
        """True iff every expected member has recorded ANY status (terminal
        or not). Use :meth:`terminal_only` to gate the next-wave dispatch
        on all-terminal."""
        return not self.outstanding()

    def terminal_only(self) -> bool:
        """True iff every expected member has reported a TERMINAL-ONLY
        status (``done`` or ``abandoned``). Non-terminal statuses
        (``blocked``, ``needs-input``) mean the wave is not yet ready to
        close — PM may need to re-dispatch or answer."""
        if not self.is_complete():
            return False
        return all(s in TERMINAL_ONLY_STATUSES for s in self.reports.values())

    def summary(self) -> dict[str, Any]:
        """Snapshot suitable for JSON serialisation / log emission."""
        return {
            "wave_id": self.wave_id,
            "expected": sorted(self.expected),
            "reports": dict(self.reports),
            "outstanding": sorted(self.outstanding()),
            "complete": self.is_complete(),
            "terminal_only": self.terminal_only(),
        }


def read_heartbeats(db_path: str | Path) -> list[tuple[str, str, str]]:
    """Read ``(team_id, role_id, last_seen_iso)`` heartbeat tuples from the
    bridge log.

    v1 surface: per role_id, returns the MOST RECENT ``kind='heartbeat'``
    bridge_messages row's ``created_at``. The heartbeat clause in the
    rules SKILL says heartbeats are informational liveness for v1 — the
    wall-clock cap is the binding stall trigger. This function exists
    so the wave-5 scheduler has a stable read surface to consume.

    Returns a list sorted by ``(team_id, role_id)`` for deterministic
    output. Empty list (not an exception) when no heartbeats exist.
    """
    db = sqlite3.connect(str(db_path))
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT team_id,
                   sender_id AS role_id,
                   MAX(created_at) AS last_seen_iso
            FROM bridge_messages
            WHERE kind = 'heartbeat'
            GROUP BY team_id, sender_id
            ORDER BY team_id, sender_id
            """
        ).fetchall()
    finally:
        db.close()
    return [(r["team_id"], r["role_id"], r["last_seen_iso"]) for r in rows]


# ── Mode-specific dispatch seam (atelier#61) ───────────────────────────────
#
# scripts/pm_dispatch.py's WaveDispatcher is MODE-AGNOSTIC: it reaches the
# outside world only through three injected seams (``spawn_fn`` / ``poll_fn`` /
# ``escalate_fn``) and carries zero mode knowledge — its ``spawn_fn`` docstring
# literally says "atelier#61 owns spawning". This section IS that owner: it
# turns the abstract "start one worker attempt" into the concrete mode-specific
# tool action — an ``Agent`` spawn in sub-agent mode, or a
# ``TeamCreate``-then-``Agent``/``SendMessage`` sequence in agent-team mode.
#
# ── Why an injected Protocol rather than direct tool calls ──
# ``scripts/dispatch.py`` is pure Python. It CANNOT call the Claude Code harness
# tools (``Agent`` / ``TeamCreate`` / ``SendMessage``) directly — those exist
# only inside an active Claude Code agent context. So every tool action is
# routed through an injected :class:`DispatchTools` boundary: a ``Protocol`` the
# orchestrator/bridge binds in production and TESTS fake with a recorder. This
# mirrors kaizen's ``scripts/team_executor.py::TeamTools`` seam (same shape:
# an injected Protocol of the minimal tool methods, faked in tests, bound to a
# queue-bridge wrapper in production).
#
# ── First-touch rule (mirrors kaizen #59) ──
# CC team-mode does NOT auto-spawn a teammate on ``SendMessage`` — sending to an
# un-spawned teammate just appends to a JSON inbox and the recipient never wakes
# up. So the FIRST send to each teammate MUST be an ``Agent`` spawn
# (run_in_background) carrying the full briefing as the prompt; SUBSEQUENT sends
# use ``SendMessage``. We decide which by reading
# ``<teams_root>/<team_id>/config.json`` and inspecting its ``members[].name``
# list: a role-id already in ``members`` => already spawned => ``SendMessage``;
# absent (or a missing/malformed config.json) => first-touch => ``Agent`` spawn.
#
# ── SCOPE (deliberate deferral — see #61) ──
# IN scope here: the mode-branching decision logic, the injected
# :class:`DispatchTools` Protocol, first-touch detection, the
# WaveDispatcher-compatible :func:`build_spawn_fn` factory, and the minimal
# :func:`resolve_dispatch_mode` read-side. This mirrors how #60 shipped
# WaveDispatcher — a tested seam with the PRODUCTION BINDING DEFERRED.
#
# OUT of scope here (a separate follow-up issue owns these — NOT an accidental
# omission):
#   * the production queue-bridge transport (a ``bridge_requests`` table, a new
#     DB migration, an orchestrator polling daemon/skill that turns enqueued
#     requests into real ``Agent``/``TeamCreate``/``SendMessage`` calls) — the
#     analog of kaizen's ``QueueBridgeWrapper``;
#   * the ``poll_fn`` / terminal-reply-envelope read implementation (the read
#     half of the WaveDispatcher seam — owned by the reply-collection follow-up);
#   * any LIVE WaveDispatcher production construction — every instantiation today
#     is in tests, exactly as #60 left it.
# The authoritative /atelier:run dispatch-mode SELECTION is owned by sibling
# #62; :func:`resolve_dispatch_mode` here is a tiny env-var stopgap, NOT that UI.


#: The two canonical dispatch-mode string values. ``"subagent"`` =>
#: fire-and-forget ``Agent`` spawns (one process per worker attempt, no team).
#: ``"agent-team"`` => a single ``TeamCreate`` per cycle, then per-task
#: ``Agent``-spawn-on-first-touch / ``SendMessage``-thereafter. We accept the
#: issue's ``"agent-team"`` spelling as canonical (hyphenated, matching the
#: issue title) and reject every other value fail-loud.
DISPATCH_MODE_SUBAGENT = "subagent"
DISPATCH_MODE_AGENT_TEAM = "agent-team"
VALID_DISPATCH_MODES: frozenset[str] = frozenset({DISPATCH_MODE_SUBAGENT, DISPATCH_MODE_AGENT_TEAM})

#: The task-dict key carrying the worker's primary id. Mirrors
#: ``pm_dispatch._task_id`` (which reads ``task["id"]``); pinned as a constant
#: so the two modules cannot drift on the key name.
_DISPATCH_TASK_ID_KEY = "id"

#: Env var the read-side consults as the HIGHEST-precedence override. Born as
#: the #61 stopgap; #62 keeps it as the explicit operator override (env beats
#: the persisted marker beats the default) so a smoke/integration run can still
#: force a mode without touching the marker file.
DISPATCH_MODE_ENV_VAR = "ATELIER_DISPATCH_MODE"

#: The persisted-mode marker file (DECISION 1 of atelier#62). One line —
#: ``"subagent"`` or ``"agent-team"`` — written under ``<root>/.ai/`` by
#: :func:`persist_dispatch_mode` (the ``persist-mode`` CLI subcommand the
#: /atelier:run skill calls after the user picks a mode). Lives under ``.ai/``
#: alongside ``atelier.db`` — the per-workspace state dir — so the choice is
#: scoped to the workspace, gitignored, and rebuilt per run rather than
#: leaking across repos via the environment.
DISPATCH_MODE_MARKER_RELPATH = Path(".ai") / "atelier.mode"

#: Default ``~/.claude/teams`` root where CC writes each team's
#: ``<team_id>/config.json``. Injectable on every function below so tests pass a
#: ``tmp_path`` rather than monkeypatching ``$HOME``.
DEFAULT_TEAMS_ROOT = Path.home() / ".claude" / "teams"


class DispatchTools(Protocol):
    """The injected tool boundary mode-specific dispatch routes through.

    ``scripts/dispatch.py`` is pure Python and cannot call the Claude Code
    harness tools directly — so every tool action goes through this Protocol.
    PRODUCTION later binds a wrapper that performs the real
    ``Agent``/``TeamCreate``/``SendMessage`` calls (via the deferred
    queue-bridge transport — see the section docstring); TESTS fake it with a
    call-recorder. This mirrors kaizen's
    :class:`scripts.team_executor.TeamTools` seam.

    Every method is synchronous from the dispatcher's point of view: spawns are
    fire-and-forget (``run_in_background`` semantics), and the worker's terminal
    reply is read back through the WaveDispatcher's SEPARATE ``poll_fn`` seam,
    never as a return value here. (That is why none of these methods return a
    response string — unlike kaizen's ``send_message``, which is request/reply.
    Here the reply path is the envelope-poll, not the tool return.)
    """

    def create_team(self, name: str, members: list[str]) -> str:
        """``TeamCreate`` — create a named team. Returns the ``team_id`` used to
        route subsequent spawns/sends. Called EXACTLY ONCE per cycle (see
        :func:`build_spawn_fn`)."""
        ...

    def spawn_teammate(self, team_id: str, name: str, prompt: str) -> None:
        """First-touch ``Agent`` spawn of teammate ``name`` INTO ``team_id``,
        run-in-background, fire-and-forget, with ``prompt`` as the full briefing.
        Required because CC does NOT auto-spawn on ``SendMessage`` (kaizen #59)."""
        ...

    def send_message(self, team_id: str, to: str, message: str) -> None:
        """``SendMessage`` to an ALREADY-spawned teammate ``to`` in ``team_id``.
        Only valid once the teammate appears in the team's ``members[].name``
        (i.e. a prior :meth:`spawn_teammate` materialised it)."""
        ...

    def spawn_subagent(self, task_id: Any, attempt: int, prompt: str) -> None:
        """Sub-agent mode: a fire-and-forget ``Agent`` (run-in-background) for
        ONE worker attempt. NOTHING team-related — no team, no membership, no
        first-touch. ``prompt`` is the full briefing."""
        ...


class UnknownDispatchModeError(DispatchError):
    """Raised when a dispatch mode is not one of :data:`VALID_DISPATCH_MODES`.

    A :class:`DispatchError` subclass (operator-facing fail-loud), mirroring
    how ``pm_dispatch.NullParallelGroupError`` subclasses it — a bad mode is a
    configuration error to fix at source, not a worker outcome to absorb.
    """

    def __init__(self, mode: Any) -> None:
        self.mode = mode
        super().__init__(
            f"unknown dispatch mode {mode!r}; expected one of "
            f"{sorted(VALID_DISPATCH_MODES)}. (Mode selection is owned by "
            "atelier#62; ATELIER_DISPATCH_MODE is the #61 stopgap.)"
        )


def _read_mode_marker(root: Path | str = ".") -> str | None:
    """Read the persisted dispatch-mode marker under ``<root>/.ai/atelier.mode``.

    Returns the validated mode string on a clean hit, or ``None`` when the
    marker is absent or blank (so :func:`resolve_dispatch_mode` falls through
    to the default). A marker that exists but carries a NON-canonical value
    is a real misconfiguration — it raises :class:`UnknownDispatchModeError`
    rather than being silently ignored, matching the env-var contract (a typo
    fails loud, it does not quietly select the default).

    Read errors other than "absent" (e.g. a directory where the file should
    be) collapse to ``None`` — a corrupt marker must not crash dispatch; the
    safe fallthrough is the default mode.
    """
    marker = Path(root) / DISPATCH_MODE_MARKER_RELPATH
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return None
    if not raw:
        return None
    if raw not in VALID_DISPATCH_MODES:
        raise UnknownDispatchModeError(raw)
    return raw


def persist_dispatch_mode(mode: str, root: Path | str = ".") -> None:
    """Persist ``mode`` to the marker file ``<root>/.ai/atelier.mode``.

    DECISION 1 of atelier#62: the /atelier:run skill, after asking the user to
    pick a dispatch mode, calls this (via the ``persist-mode`` CLI subcommand)
    so the later pure-Python dispatch can read the choice back through
    :func:`resolve_dispatch_mode` without re-prompting.

    ``mode`` is validated against :data:`VALID_DISPATCH_MODES` BEFORE any write
    — an invalid mode raises :class:`UnknownDispatchModeError` and leaves the
    marker untouched, so a typo never persists a wedged state. The ``.ai/``
    directory is created if absent. The file is a single line (the mode +
    trailing newline) so it is trivially human-inspectable and `cat`-able.
    """
    if mode not in VALID_DISPATCH_MODES:
        raise UnknownDispatchModeError(mode)
    marker = Path(root) / DISPATCH_MODE_MARKER_RELPATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{mode}\n", encoding="utf-8")


def resolve_dispatch_mode(env: Mapping[str, str] = os.environ, root: Path | str = ".") -> str:
    """Resolve the dispatch mode by precedence: env override → marker → default.

    atelier#62 makes this the authoritative read-side. Precedence (highest
    first):

    1. :data:`DISPATCH_MODE_ENV_VAR` (``ATELIER_DISPATCH_MODE``) — the explicit
       operator override (kept from the #61 stopgap for smoke/integration runs
       and back-compat). A set-but-blank value is treated as unset.
    2. The persisted marker ``<root>/.ai/atelier.mode`` written by
       :func:`persist_dispatch_mode` (the /atelier:run mode the user picked).
    3. :data:`DISPATCH_MODE_SUBAGENT` — the default when neither speaks.

    A non-canonical value in EITHER the env var or the marker raises
    :class:`UnknownDispatchModeError` (a typo fails loud rather than silently
    selecting the default). ``root`` is injectable so tests pass a ``tmp_path``
    rather than relying on CWD.
    """
    raw = (env.get(DISPATCH_MODE_ENV_VAR) or "").strip()
    if raw:
        if raw not in VALID_DISPATCH_MODES:
            raise UnknownDispatchModeError(raw)
        return raw
    marked = _read_mode_marker(root)
    if marked is not None:
        return marked
    return DISPATCH_MODE_SUBAGENT


def _team_member_names(team_id: str, teams_root: Path | str = DEFAULT_TEAMS_ROOT) -> set[str]:
    """Return the set of ``members[].name`` from ``<teams_root>/<team_id>/config.json``.

    First-touch detection (kaizen #59) hinges on this: a teammate whose role-id
    is in this set has already been spawned (=> ``SendMessage``); one that is
    absent has not (=> first-touch ``Agent`` spawn).

    Graceful by contract — a MISSING config.json (CC has not written it yet, or
    the team has no members) is treated as "no members yet" (empty set =>
    first-touch), NOT an error. Malformed JSON, a non-dict root, a non-list
    ``members``, or a member entry without a string ``name`` are all tolerated
    the same way: skip the bad entry / return what is parseable. We never raise
    here — a read error must not crash a dispatch; the worst case is a redundant
    re-spawn, which is strictly safer than a never-delivered ``SendMessage`` to
    an un-spawned teammate.
    """
    cfg_path = Path(teams_root) / team_id / "config.json"
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, Mapping):
        return set()
    members = data.get("members")
    if not isinstance(members, list):
        return set()
    names: set[str] = set()
    for member in members:
        if isinstance(member, Mapping):
            name = member.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def dispatch_task(
    mode: str,
    *,
    tools: DispatchTools,
    task: Mapping[str, Any],
    attempt: int,
    briefing: str,
    team_id: str | None = None,
    teammate_name: str | None = None,
    teams_root: Path | str = DEFAULT_TEAMS_ROOT,
) -> None:
    """Dispatch ONE worker attempt, branching on ``mode`` internally.

    This is the single mode-branching entry point (#61 acceptance criterion 1):
    the SAME mode-agnostic ``briefing`` text feeds either path — the decision is
    purely about WHICH TOOL CALL to make, never about the prompt content (see
    :func:`compose_briefing`, which stays mode-agnostic).

    * ``mode == "subagent"`` — ``tools.spawn_subagent(task_id, attempt,
      briefing)``. Fire-and-forget ``Agent`` (run-in-background). Nothing
      team-related; ``team_id`` / ``teammate_name`` are ignored.
    * ``mode == "agent-team"`` — first-touch detected by reading
      ``<teams_root>/<team_id>/config.json`` members[].name (missing file =>
      first-touch):
        - first-touch     => ``tools.spawn_teammate(team_id, teammate_name,
          briefing)`` (an ``Agent`` spawn — NOT a naked ``SendMessage``, which
          CC would silently drop into an inbox; kaizen #59);
        - already spawned => ``tools.send_message(team_id, to=teammate_name,
          message=briefing)``.
      Requires both ``team_id`` and ``teammate_name``.
    * any other ``mode`` => :class:`UnknownDispatchModeError`.

    Note the asymmetry vs. ``compose_briefing``: this function does NOT build
    the briefing — the caller (or :func:`build_spawn_fn`) passes it pre-rendered
    so the mode-agnostic composer and the mode-specific dispatcher stay cleanly
    separated.
    """
    if mode == DISPATCH_MODE_SUBAGENT:
        tools.spawn_subagent(task[_DISPATCH_TASK_ID_KEY], attempt, briefing)
        return
    if mode == DISPATCH_MODE_AGENT_TEAM:
        if team_id is None or teammate_name is None:
            raise DispatchError(
                "agent-team dispatch requires both team_id and teammate_name; "
                f"got team_id={team_id!r}, teammate_name={teammate_name!r}"
            )
        already_spawned = teammate_name in _team_member_names(team_id, teams_root)
        if already_spawned:
            tools.send_message(team_id, to=teammate_name, message=briefing)
        else:
            # First-touch: MUST be an Agent spawn, never a naked SendMessage
            # (CC would drop it into an inbox the un-spawned teammate never
            # reads — kaizen #59).
            tools.spawn_teammate(team_id, teammate_name, briefing)
        return
    raise UnknownDispatchModeError(mode)


def build_spawn_fn(
    mode: str,
    *,
    tools: DispatchTools,
    briefing_for: Callable[[Mapping[str, Any], int], str],
    members: list[str] | None = None,
    team_name: str | None = None,
    teammate_name_for: Callable[[Mapping[str, Any]], str] | None = None,
    teams_root: Path | str = DEFAULT_TEAMS_ROOT,
) -> Callable[[Mapping[str, Any], int], None]:
    """Build a WaveDispatcher-compatible ``spawn_fn(task, attempt) -> None``.

    The returned callable matches ``pm_dispatch.WaveDispatcher``'s ``spawn_fn``
    seam exactly, so a later production-binding issue can drop it straight in —
    WITHOUT wiring a live WaveDispatcher here (that construction stays deferred,
    per #60's precedent and this module's SCOPE note).

    ``briefing_for(task, attempt) -> str`` is the briefing source: production
    passes a ``compose_briefing`` wrapper (keeping the composer mode-agnostic);
    tests pass a trivial stub. Each spawn re-renders via this callable so an
    attempt-specific briefing is possible.

    For ``mode == "agent-team"`` the factory enforces **TeamCreate-once**: it
    calls ``tools.create_team(team_name, members)`` LAZILY on the FIRST spawn
    and captures the returned ``team_id`` in the closure; every subsequent spawn
    reuses that id (so ``create_team`` fires exactly once per cycle regardless of
    how many tasks/attempts the wave engine dispatches). ``team_name`` and
    ``members`` are required for agent-team mode. ``teammate_name_for(task) ->
    str`` maps a task to its teammate role-id (defaults to ``str(task["id"])``).

    For ``mode == "subagent"`` no team is created and ``team_name`` / ``members``
    are ignored.

    Raises :class:`UnknownDispatchModeError` immediately on an invalid mode (so
    a misconfiguration fails at factory-build time, not on the first dispatch).
    """
    if mode not in VALID_DISPATCH_MODES:
        raise UnknownDispatchModeError(mode)

    if mode == DISPATCH_MODE_AGENT_TEAM and team_name is None:
        raise DispatchError(
            "agent-team mode requires team_name to build the spawn_fn "
            "(TeamCreate is called once per cycle with it)."
        )

    name_for = teammate_name_for or (lambda task: str(task[_DISPATCH_TASK_ID_KEY]))

    # Mutable cell capturing the once-per-cycle team_id. A list is the minimal
    # closure-mutable container; ``team_id_cell[0] is None`` is the "not yet
    # created" sentinel that gates the single create_team call.
    team_id_cell: list[str | None] = [None]

    def spawn_fn(task: Mapping[str, Any], attempt: int) -> None:
        if mode == DISPATCH_MODE_SUBAGENT:
            dispatch_task(
                DISPATCH_MODE_SUBAGENT,
                tools=tools,
                task=task,
                attempt=attempt,
                briefing=briefing_for(task, attempt),
            )
            return
        # agent-team — create the team exactly once, lazily, then reuse the id.
        if team_id_cell[0] is None:
            team_id_cell[0] = tools.create_team(team_name, list(members or []))
        dispatch_task(
            DISPATCH_MODE_AGENT_TEAM,
            tools=tools,
            task=task,
            attempt=attempt,
            briefing=briefing_for(task, attempt),
            team_id=team_id_cell[0],
            teammate_name=name_for(task),
            teams_root=teams_root,
        )

    return spawn_fn


# ── CLI ────────────────────────────────────────────────────────────────────


def _read_text_or_at_path(value: str) -> str:
    """Resolve a ``--task-brief`` / ``--persona`` / ``--phase-procedure``
    argument. ``@<path>`` is read from disk; everything else is taken
    verbatim. Mirrors the convention in scripts/bridge_send.py."""
    if value.startswith("@"):
        path = Path(value[1:])
        return path.read_text(encoding="utf-8")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dispatch",
        description=(
            "Atelier team-mode worker dispatch — compose briefings, validate render context."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    compose = sub.add_parser(
        "compose",
        help="Compose + render a worker briefing to stdout (smoke-test surface).",
    )
    compose.add_argument("--role", required=True, help="role_id of the worker")
    compose.add_argument("--task-id", required=True, help="tasks.id this dispatch is against")
    compose.add_argument(
        "--task-brief",
        required=True,
        help="Task narrative (or @path to a file).",
    )
    compose.add_argument(
        "--persona",
        default="(persona profile body would be loaded from the agents DB here)",
        help="Persona profile body (or @path). CLI default is a placeholder for smoke tests.",
    )
    compose.add_argument(
        "--phase-procedure",
        default="(phase procedure body would be loaded from internal/<phase>/SKILL.md here)",
        help="Phase procedure body (or @path). CLI default is a placeholder for smoke tests.",
    )
    compose.add_argument("--team", default="smoke-team", help="team_id (default: smoke-team)")
    compose.add_argument(
        "--team-lead",
        default="team-lead",
        help="member_name of the team-lead (default: team-lead)",
    )
    compose.add_argument("--wave", default="wave-1", help="wave_id (default: wave-1)")
    compose.add_argument(
        "--wave-phase",
        default="implement",
        help="wave phase label (default: implement)",
    )
    compose.add_argument(
        "--deadline",
        default="2099-01-01T00:00:00Z",
        help="ISO-8601 UTC deadline (default: far-future smoke value)",
    )
    compose.add_argument(
        "--out",
        default="-",
        help="Output destination ('-' for stdout, otherwise a file path).",
    )

    persist = sub.add_parser(
        "persist-mode",
        help=(
            "Persist the chosen dispatch mode to <root>/.ai/atelier.mode "
            "(called by the /atelier:run skill after the user picks a mode)."
        ),
    )
    persist.add_argument(
        "mode",
        choices=sorted(VALID_DISPATCH_MODES),
        help="The dispatch mode to persist ('subagent' or 'agent-team').",
    )
    persist.add_argument(
        "--root",
        default=".",
        help="Workspace root containing the .ai/ dir (default: CWD).",
    )
    return parser


def _cmd_compose(args: argparse.Namespace) -> int:
    task_brief = _read_text_or_at_path(args.task_brief)
    persona = _read_text_or_at_path(args.persona)
    phase_procedure = _read_text_or_at_path(args.phase_procedure)

    rendered = compose_briefing(
        role_id=args.role,
        task_id=args.task_id,
        persona_profile_text=persona,
        phase_procedure_text=phase_procedure,
        task_brief=task_brief,
        team_id=args.team,
        team_lead_name=args.team_lead,
        wave_id=args.wave,
        wave_phase=args.wave_phase,
        deadline_iso=args.deadline,
    )
    payload = {
        "role_id": args.role,
        "task_id": args.task_id,
        "briefing": rendered,
        "schema_version": SCHEMA_VERSION,
    }
    out_line = json.dumps(payload, ensure_ascii=False) + "\n"
    if args.out == "-":
        sys.stdout.write(out_line)
    else:
        Path(args.out).write_text(out_line, encoding="utf-8")
    return 0


def _cmd_persist_mode(args: argparse.Namespace) -> int:
    persist_dispatch_mode(args.mode, root=args.root)
    marker = Path(args.root) / DISPATCH_MODE_MARKER_RELPATH
    sys.stdout.write(f"dispatch: persisted mode {args.mode!r} to {marker}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "compose":
        try:
            return _cmd_compose(args)
        except (MissingRenderVarsError, UndefinedError) as exc:
            sys.stderr.write(f"dispatch: {exc}\n")
            return 1
        except DispatchError as exc:
            sys.stderr.write(f"dispatch: {exc}\n")
            return 2
    if args.cmd == "persist-mode":
        try:
            return _cmd_persist_mode(args)
        except DispatchError as exc:
            sys.stderr.write(f"dispatch: {exc}\n")
            return 2
    parser.error(f"unknown command {args.cmd!r}")
    return 2  # unreachable; argparse.error exits


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
