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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

from scripts.model_tier import normalize_phase

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

# ── B1 — always-on terse-output rule (caveman token-compression) ──────────
#
# Additive prompt guidance appended to every COMPOSED worker briefing (see
# :func:`compose_briefing`). It asks the spawned worker to keep its free-text
# ``notes_md`` prose COMPACT (caveman style — fragments, one line per finding,
# drop articles/filler/hedging/pleasantries) so the orchestrator's context
# stays lean when the worker's reply is read back off the bridge.
#
# This is pure GUIDANCE to the agent — NOT a parser change, NOT a wire-protocol
# change. It is appended to the RENDERED briefing (after Jinja2 has run), so it
# sits OUTSIDE the template's ``untrusted(...)`` TASK fence as the briefing's
# final guidance section. It does NOT touch role.j2 / _base.j2 (their byte-
# parity tests stay green) and it does NOT touch the TM-006 reply contract.
#
# HARD CARVE-OUT (mirrors caveman_codec's protected segments): the directive
# explicitly EXCLUDES the TM-006 JSON reply envelope (``task_result`` /
# ``shutdown_response`` and ALL its keys/values), code (fenced or inline), file
# paths, identifiers (CONST_CASE / dotted.names / fn() calls), version numbers,
# quoted error strings, and the ``ABANDON:`` grammar line — those stay
# byte-exact. A literal-minded LLM must never "compress" a path, an error
# string, the reply envelope JSON, or the ABANDON line.
_TERSE_OUTPUT_RULE = (
    "\n\n# OUTPUT SHAPE (terse — read last)\n\n"
    "Keep your free-text `notes_md` prose COMPACT to save the orchestrator's "
    "context. Talk like a smart caveman — brain stays big, only fluff dies. Use "
    "fragments, one line per finding/decision, no pleasantries, no hedging, no "
    "restating this briefing. Drop articles and filler where meaning survives. "
    "This applies ONLY to your free-text prose. Do NOT compress or alter, and "
    "reproduce VERBATIM: the TM-006 reply envelope (the `task_result` / "
    "`shutdown_response` JSON and ALL its keys/values), code (fenced or inline), "
    "file paths, identifiers (CONST_CASE, dotted.names, fn() calls), version "
    "numbers, quoted error strings, and the `ABANDON: <category>:<reason>` "
    "grammar line. Those stay byte-exact. If terseness would create technical "
    "ambiguity (security, destructive/irreversible actions, ordered multi-step "
    "sequences), write that part in full."
)

# ── CONTEXT-BUDGET discipline — always-on, reaches EVERY dispatched worker ──
#
# The HONEST mechanism (read this before assuming the hooks cover you): atelier's
# PostToolUse nudge (``hooks/context_budget.py``, 125k) and PreCompact snapshot
# (``hooks/pre_compact.py``) fire ONLY in the ORCHESTRATOR's interactive session,
# scoped to ``.ai/active_project`` presence. A worker the deterministic host
# spawns one-shot (``compose_briefing`` → a ``claude -p`` CLI subprocess) does
# NOT inherit those hooks — its working context is independent and is NOT
# auto-managed by atelier. So the ONLY context-budget signal that reaches a
# worker is THIS briefing text. This rule is that signal: it tells the worker to
# checkpoint-then-wind-down near the threshold so a subagent does not silently
# blow past ~150k mid-task (the measured pain this lever closes).
#
# Claude Code has NO native "auto-compact at a token threshold" trigger and a
# one-shot subagent cannot self-fire ``/compact``; therefore this is honest
# AGENT-ACTED discipline (checkpoint first, then wind down + return), NOT a claim
# of silent automatic compaction.
#
# Threshold (125000) is kept consistent with
# ``hooks/context_budget.py::DEFAULT_THRESHOLD_TOKENS`` — keep them in sync if it
# changes (CLAUDE.md ``## Auto-trigger architecture`` documents the contract).
#
# Same append discipline as ``_TERSE_OUTPUT_RULE``: opens with "\n\n" + its own
# ``# CONTEXT BUDGET`` heading, appended to the RENDERED briefing OUTSIDE the
# template's ``untrusted`` TASK fence (guidance, never injectable task data). It
# does NOT touch role.j2 / _base.j2 (their byte-parity tests stay green) and does
# NOT touch the TM-006 reply envelope.
_CONTEXT_BUDGET_RULE = (
    "\n\n# CONTEXT BUDGET (read last)\n\n"
    "Your working context is INDEPENDENT and is NOT auto-managed by atelier's "
    "hooks — the PostToolUse 125k nudge and PreCompact snapshot fire only in the "
    "orchestrator's interactive session, not inside your spawn. So YOU must act: "
    "when your context approaches ~125000 tokens, do BOTH, in order. (1) FIRST "
    "write a durable structured checkpoint of your decisions, blockers, and "
    "partial-progress — either a short file in your working dir (e.g. "
    "`.ai/subagent-checkpoints/<role>-checkpoint.md`) or your returned structured "
    "summary — so nothing is lost. (2) THEN wind down and RETURN your terminal "
    "`task_result` / structured summary rather than continuing to accumulate past "
    "~150000 tokens. Claude Code cannot silently auto-compact a one-shot subagent "
    "at a threshold, so this is your responsibility — checkpoint first, then "
    "return; do not just keep going."
)

# Output-side "minimal-diff / native-first" implementer lever (M8 rec #3, ponytail
# analysis). UNLIKE the always-on terse/context-budget rules, this one is GATED to
# implementation phases (tdd / tdd:green / tdd:clean) AND toggleable via
# ``compose_briefing(include_minimal_diff=...)`` — a designer/reviewer/test-author
# must NOT be told to write minimal code. Same append discipline: opens with
# "\n\n# HEADING", appended to ``rendered.rstrip()`` OUTSIDE the untrusted TASK
# fence (guidance, never injectable task data); does NOT touch role.j2/_base.j2 or
# the TM-006 reply envelope. It is the output-side lever atelier lacked — every
# prior lever is input-side and none constrains what an implementer BUILDS.
_MINIMAL_DIFF_RULE = (
    "\n\n# WHAT TO BUILD (minimal-diff — implementer, read last)\n\n"
    "You are implementing. Prefer the SMALLEST change that satisfies the task "
    "and its acceptance criteria. Walk this ladder and STOP at the first rung "
    "that holds: (1) YAGNI — do not build what the task did not ask for; "
    "(2) the standard library; (3) a native platform/framework feature; "
    "(4) an already-installed dependency; (5) one line; (6) minimum custom "
    "code. Do not add a new dependency, abstraction, config flag, or layer "
    "when a lower rung already works. "
    "REFLEX, NOT RESEARCH: the ladder is a quick reflex, not a research "
    "project — if two rungs both work, take the higher (lazier) one and move "
    "on; ship the lazy version and question it in the SAME response rather "
    "than deliberating across turns (deliberation burns the very tokens this "
    "rule exists to save). "
    "WHEN NOT TO BE LAZY (load-bearing carve-out): do NOT minimize away input "
    "validation at trust boundaries, error handling that prevents data loss, "
    "security, accessibility, or anything the task EXPLICITLY requested. Leave "
    "ONE runnable check (a test or assertion) behind any non-trivial logic. "
    "The lazy choice is the small SAFE choice, never the unsafe one — when "
    "minimizing would drop a guard, keep the guard. "
    "This guidance shapes the CODE you write; it does NOT change the TM-006 "
    "reply envelope, the acceptance criteria, or anything inside the TASK "
    "fence above."
)

# Implementation phases the minimal-diff lever gates ON. NOT design/plan/review/
# security/tdd:red/qa/verify/doc — a non-implementer (designer, reviewer, test
# author) must never be told to minimize code. Gate on normalize_phase MEMBERSHIP,
# never on tier (qa/verify are ALSO sonnet) and never on a bare == (the live host
# value is "tdd:green", which a bare == "tdd" would miss → an inert lever).
_IMPLEMENTATION_PHASES: frozenset[str] = frozenset({"tdd", "tdd:green", "tdd:clean"})


def _is_implementation_phase(wave_phase: str) -> bool:
    """True iff ``wave_phase`` is a code-writing implementation phase — the only
    phases the minimal-diff lever applies to. Uses ``model_tier.normalize_phase``
    so ``dev:tdd`` / ``tdd-green`` / etc. canonicalize to the gate keys."""
    return normalize_phase(wave_phase) in _IMPLEMENTATION_PHASES


# ── ATELIER_TRANSPORT — cli (the deterministic host; the ONLY transport) ────
#
# The transport selector. Since M7 PR-B the SQLite-bridge dispatch QUEUE is
# DELETED, so ``cli`` (the deterministic host) is the only valid transport: each
# attempt is one metered ``claude -p --json-schema`` subprocess whose terminal
# ``structured_output`` IS the reply envelope (no bridge wire, no
# ``bridge_send.py``). The legacy ``ATELIER_TRANSPORT=bridge`` escape hatch is
# GONE — selecting it (or any other value) now fails loud via
# :class:`UnknownTransportError`. (Note: the inter-agent message WIRE —
# ``bridge_messages`` / ``bridge_send.py`` / ``bridge_read.py`` / ``team_meeting``
# / ``status`` — is unrelated to this dispatch-transport selector and STAYS; only
# the dispatch QUEUE was removed.)
TRANSPORT_ENV_VAR = "ATELIER_TRANSPORT"
TRANSPORT_CLI = "cli"
VALID_TRANSPORTS: frozenset[str] = frozenset({TRANSPORT_CLI})


def resolve_transport(env: Mapping[str, str] = os.environ) -> str:
    """Resolve the dispatch transport: ``ATELIER_TRANSPORT`` env → ``cli``.

    Returns ``"cli"`` (the deterministic-host pipeline — the ONLY transport since
    the M7 bridge-queue removal) when ``ATELIER_TRANSPORT`` is unset / empty /
    whitespace. Any explicit value OTHER than ``cli`` — including the retired
    ``bridge`` escape hatch — raises :class:`UnknownTransportError` (fail-loud: a
    typo, or a stale ``ATELIER_TRANSPORT=bridge`` in someone's shell, must not
    silently select a transport that no longer exists).
    """
    raw = (env.get(TRANSPORT_ENV_VAR) or "").strip()
    if not raw:
        return TRANSPORT_CLI
    if raw not in VALID_TRANSPORTS:
        raise UnknownTransportError(raw)
    return raw


# ── The transport ROUTING predicate (deterministic host) ─────────────────────
#
# `resolve_transport` re-points the BRIEFING TEXT (the CLI CHANNELS/REPLY-CONTRACT
# addendum in `compose_briefing`) AND gates the dispatch path. Since the M7
# bridge-queue removal there is only one path: the M5 host `pipeline()`. The
# PRODUCTION entrypoint is a SKILL recipe the orchestrator drives
# (`internal/dev-dispatch/SKILL.md`); :func:`is_host_transport` is the predicate
# that recipe (and tests) consult, and :func:`dispatch_host_pipeline` is the thin
# Python seam it routes through. There is no longer a bridge `WaveDispatcher`
# factory branch — the host pipeline is unconditional.


def is_host_transport(env: Mapping[str, str] = os.environ) -> bool:
    """True iff the resolved transport is the M5/M6 deterministic-host (CLI) path.

    A tiny, side-effect-free predicate the orchestrator recipe (and tests) use to
    confirm the dispatch path: ``cli`` (the only transport since the M7
    bridge-queue removal) → :func:`scripts.host_scheduler.run_host_pipeline_for_project`.
    Fail-loud on an unknown transport (delegates to :func:`resolve_transport`,
    which now rejects the retired ``bridge`` value).
    """
    return resolve_transport(env) == TRANSPORT_CLI


async def dispatch_host_pipeline(
    tasks: Any,
    *,
    clone_dir: Any,
    budget: Any,
    journal: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Route a dispatch through the deterministic host pipeline (``cli`` transport).

    The thin production seam the orchestrator recipe calls when
    :func:`is_host_transport` is true. Delegates to
    :func:`scripts.host_scheduler.run_host_pipeline_for_project` (the M6a
    production caller that composes the recommend-backed CLI dispatch factory with
    the M5 ``pipeline()`` scheduler). Kept here so the transport-selection logic
    lives next to :func:`resolve_transport`; lazy-imports ``host_scheduler`` to
    keep its (heavy) chain off the bare import path. This is now the ONLY dispatch
    path — the legacy bridge WaveDispatcher factory was removed in M7 PR-B.
    """
    from scripts.host_scheduler import run_host_pipeline_for_project

    return await run_host_pipeline_for_project(
        tasks, clone_dir=clone_dir, budget=budget, journal=journal, **kwargs
    )


# ── CLI-mode CHANNELS / REPLY-CONTRACT addendum ────────────────────────────
#
# In `cli` transport (the only transport since the M7 bridge-queue removal) the
# worker is a one-shot `claude -p --json-schema` call: there is NO dispatch
# bridge, NO `bridge_send.py` reply, NO peer wire. Its terminal
# `structured_output` (matching ENVELOPE_SCHEMA) IS its reply. This addendum
# re-points the template's CHANNELS/REPLY-CONTRACT guidance accordingly. It is
# APPENDED to the rendered briefing (same discipline as `_TERSE_OUTPUT_RULE` /
# `_CONTEXT_BUDGET_RULE`: opens with "\n\n", its own heading, OUTSIDE the
# template's untrusted TASK fence). The template still renders its historical
# bridge CHANNELS block first (the inter-agent message wire still exists); this
# addendum tells the one-shot CLI worker to IGNORE it and return structured
# output instead.
_CLI_TRANSPORT_RULE = (
    "\n\n# TRANSPORT OVERRIDE — CLI MODE (read last; SUPERSEDES the CHANNELS + "
    "REPLY CONTRACT sections above)\n\n"
    "You are a one-shot, ephemeral agent. There is NO bridge, NO `bridge_send.py` "
    "/ `bridge_read.py`, and NO live peers to message — IGNORE every "
    "`bridge_send.py` / `bridge_read.py` / heartbeat command in the CHANNELS "
    "section above; those commands do not exist in this run. Do NOT attempt "
    "inter-agent messaging.\n\n"
    "RETURN YOUR RESULT as the structured final message matching the provided "
    "json-schema (the TM-006 `task_result` envelope: `type` / `task_id` / "
    "`attempt` / `status` / `artifacts` / `notes_md`). That structured output IS "
    "your reply to the team-lead — the deterministic host reads it directly as "
    "the return value of your invocation; you do not send it anywhere. Emit it "
    "exactly once, as your terminal message. The abandon grammar, the four "
    "closure tokens, and the artifacts contract are UNCHANGED — only the delivery "
    "channel changes (structured return value, not a bridge send)."
)


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
        # team_chat — the OPTIONAL Loom-vs-bridge chat-transport ctx (atelier
        # loom-team-comms). ALWAYS a non-None dict: compose_briefing coerces
        # None → {"transport": "bridge"}, so validate_render_context (None ==
        # missing) passes on the fallback path and the AST-union test stays
        # consistent. The bridge reply-envelope wiring (bridge_cmds) is
        # UNAFFECTED — Loom never carries the control-plane.
        "team_chat",
    }
)

# Terminal closure tokens from TM-006. The wave tracker uses this set to
# decide whether a member's last status counts as "reported"; the three
# terminal-only tokens are `done`, `abandoned`, and `failed`.
#
# `failed` (the bounded-path hardening feature) is a DETERMINISTIC run-and-failed
# signal — the worker ran and hit a hard failure — distinct from the RETRYABLE
# `blocked`/`needs-input`. It CLOSES the wave (terminal-only) and routes to a
# terminal handler that records + escalates WITHOUT consuming MAX_ATTEMPTS retries
# (a hard failure is not worth re-dispatching). This set is SINGLE-SOURCED here;
# pm_dispatch_envelope.py and pm_dispatch.py import it — never re-type the tokens.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"done", "blocked", "abandoned", "needs-input", "failed"}
)
TERMINAL_ONLY_STATUSES: frozenset[str] = frozenset({"done", "abandoned", "failed"})


# ── Exceptions ─────────────────────────────────────────────────────────────


class DispatchError(RuntimeError):
    """Base class for explicit dispatch failures. Subclasses carry actionable
    messages so the operator (or the calling skill) can fix at source rather
    than chase a generic stack trace."""


class UnknownTransportError(DispatchError):
    """Raised by :func:`resolve_transport` when ``ATELIER_TRANSPORT`` carries a
    value outside :data:`VALID_TRANSPORTS`.

    A :class:`DispatchError` subclass (operator-facing fail-loud), mirroring
    :class:`UnknownDispatchModeError`: a bad transport is a configuration error
    to fix at source, not a worker outcome to absorb. A typo must fail loud, it
    must not silently select the default. Since M7 PR-B the only valid transport
    is ``cli`` — the legacy ``bridge`` value is REMOVED and lands here too, so a
    stale ``ATELIER_TRANSPORT=bridge`` surfaces loudly rather than selecting a
    dispatch queue that no longer exists.
    """

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        bridge_hint = (
            " The 'bridge' dispatch queue was removed in M7 — 'cli' (the "
            "deterministic host) is the only transport; unset ATELIER_TRANSPORT "
            "to use it."
            if str(transport) == "bridge"
            else ""
        )
        super().__init__(
            f"unknown transport {transport!r}; expected one of "
            f"{sorted(VALID_TRANSPORTS)} (env var {TRANSPORT_ENV_VAR})."
            f"{bridge_hint}"
        )


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
    team_chat: Mapping[str, Any] | None = None,
    idempotency_seed: str | None = None,
    from_agent_self: str | None = None,
    template_env: Environment | None = None,
    transport: str | None = None,
    include_terse: bool = True,
    include_minimal_diff: bool = True,
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

    ``team_chat`` is the OPTIONAL chat-transport ctx (atelier loom-team-comms):
    a ``{"transport": "loom"|"bridge", ...}`` mapping built by
    ``scripts.loom_comms.build_team_chat_context``. ``None`` is coerced to
    ``{"transport": "bridge"}`` so EXISTING callers are byte-stable — they
    render the identical bridge CHANNELS block and NO Loom subsection. When a
    ``loom``-transport dict is passed, the template renders the additional Loom
    chat protocol; the bridge reply-envelope wiring (``bridge_cmds``) is
    UNAFFECTED in either case — Loom never carries the control-plane
    (``task_result`` / TM-006 always rides the bridge).

    ``transport`` selects the dispatch transport. ``None`` (the default) resolves
    ``ATELIER_TRANSPORT`` from the environment via :func:`resolve_transport`,
    which → ``"cli"`` (the only transport since the M7 bridge-queue removal). In
    ``"cli"`` transport a CLI CHANNELS/REPLY-CONTRACT addendum
    (:data:`_CLI_TRANSPORT_RULE`) is appended AFTER the terse + context-budget
    rules, re-pointing "use bridge_send.py" → "return your result as the
    structured final message matching the json-schema". Any value other than
    ``"cli"`` (including the retired ``"bridge"``) raises
    :class:`UnknownTransportError`. The ``team_chat`` / loom wiring is independent
    of this and untouched (the inter-agent message WIRE, distinct from the removed
    dispatch queue, still rides ``bridge_send.py``/``bridge_read.py``).

    ``include_terse`` (default ``True``) gates the appended ``_TERSE_OUTPUT_RULE``
    + ``_CONTEXT_BUDGET_RULE`` tail; the default path is byte-identical to today,
    and ``_CLI_TRANSPORT_RULE`` is NOT gated (transport-correctness, not a
    measurement lever). SCOPE (M8 lever foundation): the flag is presently set only
    via a direct call / the A/B tests — there is no env / run_mode wiring yet (the
    two production callers of ``_host_briefing_for`` omit it, so a live run is
    always ``True``); operator wiring lands with the measurement harness. It gates
    only the APPENDED ``_CONTEXT_BUDGET_RULE`` constant: the equivalent
    context-budget discipline subsection in ``internal/team-mode-rules/SKILL.md`` is
    always rendered, so ``include_terse=False`` is a clean control for the terse
    rule and removes the appended budget tail, but does NOT remove the rules-block
    budget guidance (single-sourcing that is a follow-up).

    ``include_minimal_diff`` (default ``True``) gates the output-side
    ``_MINIMAL_DIFF_RULE`` (the minimal-diff/native-first ladder + anti-deliberation
    reflex + safety carve-out, M8 rec #3). UNLIKE the always-on terse/budget rules
    it is PHASE-GATED: it appends ONLY for an implementation ``wave_phase``
    (tdd / tdd:green / tdd:clean, via :func:`_is_implementation_phase`) — never for
    design / plan / review / security / tdd:red / qa / verify / doc. Default path
    for a non-implementation phase is byte-identical to today (the rule never
    appends there). Set only via a direct call / the A/B tests today — the two
    production ``_host_briefing_for`` callers omit it, so a live run is always
    ``True`` (then phase-gated); no env / run_mode wiring yet.

    Returns the fully-rendered briefing string.
    """
    transport = resolve_transport() if transport is None else transport
    if transport not in VALID_TRANSPORTS:
        raise UnknownTransportError(transport)
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
        # Sort list fields for byte-determinism so same-role retries re-render an
        # identical prefix (cycle-3 prompt-cache determinism). Order is
        # presentational only — workers read the same items.
        "peers": sorted(peers or [], key=lambda p: str(p.get("role_id", ""))),
        "quorum_rule": quorum_rule,
        "forbidden_actions": sorted(str(a) for a in (forbidden_actions or [])),
        "task_brief": composed_task_brief,
        "acceptance_criteria": sorted(str(ac) for ac in (acceptance_criteria or [])),
        # team_chat is ALWAYS a non-None dict: coerce None → bridge fallback so
        # the template's {% if team_chat.transport == 'loom' %} branch is
        # byte-stable for existing callers and validate_render_context passes.
        # NB: this team_chat.transport is the MESSAGE-WIRE label (loom | bridge),
        # a DIFFERENT axis from the dispatch transport resolved by
        # resolve_transport() (cli-only; 'bridge' raises there). The two never
        # cross — this default is the wire fallback, not a dispatch transport.
        "team_chat": dict(team_chat) if team_chat is not None else {"transport": "bridge"},
    }

    validate_render_context(ctx)

    tmpl = template_env.get_template(ROLE_TEMPLATE)
    rendered = tmpl.render(**ctx)
    # B1 — always-on terse-output guidance. Appended to the RENDERED briefing
    # (outside the template's untrusted TASK fence) as the briefing's final
    # guidance section. `_TERSE_OUTPUT_RULE` opens with "\n\n" so it reads as
    # its own paragraph; we rstrip the rendered body first so the separator is
    # exactly one clean paragraph break regardless of the template's trailing
    # whitespace. Does NOT modify role.j2 / _base.j2 or the TM-006 contract.
    #
    # CONTEXT-BUDGET discipline — also always-on, appended AFTER the terse rule
    # (deterministic, stable order: terse → context-budget). Both sit OUTSIDE the
    # untrusted TASK fence; both open with "\n\n" so each is its own paragraph.
    # This is the single load-bearing channel that reaches a one-shot worker — the
    # PostToolUse/PreCompact hooks fire only in the orchestrator session, never in
    # a spawned worker (see `_CONTEXT_BUDGET_RULE`).
    #
    # CLI-transport addendum — appended LAST, in `cli` transport (the only valid
    # transport since the M7 bridge-queue removal). It re-points the
    # CHANNELS/REPLY-CONTRACT guidance to "return the structured final message"
    # (see `_CLI_TRANSPORT_RULE`). The `if` guard is retained as a defensive
    # belt-and-braces — `transport` is already validated against VALID_TRANSPORTS
    # above, so it is always TRANSPORT_CLI here.
    body = rendered.rstrip()
    if include_terse:
        body += _TERSE_OUTPUT_RULE + _CONTEXT_BUDGET_RULE
    # Output-side minimal-diff lever (M8 rec #3) — GATED to implementation phases
    # (tdd/tdd:green/tdd:clean) and toggleable; appended AFTER the terse/budget
    # block and BEFORE the cli rule so _CLI_TRANSPORT_RULE stays the tail.
    if include_minimal_diff and _is_implementation_phase(wave_phase):
        body += _MINIMAL_DIFF_RULE
    if transport == TRANSPORT_CLI:
        body += _CLI_TRANSPORT_RULE
    return body


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
        :class:`ValueError` if ``status`` is not one of the TM-006
        closure tokens (:data:`TERMINAL_STATUSES`) — silent acceptance of a
        typo would mask a contract violation."""
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
# ── SCOPE ──
# What lives here: the mode-branching decision logic (:func:`dispatch_task`), the
# injected :class:`DispatchTools` Protocol, first-touch detection, and the
# :func:`resolve_dispatch_mode` read-side. The PRODUCTION binding of
# :class:`DispatchTools` is the deterministic host's ``CliDispatchTools``
# (``scripts/cli_dispatch.py``); the dispatch-mode SELECTION is owned by the
# /atelier:run skill (env var → persisted ``.ai/atelier.mode`` marker → default).
#
# REMOVED in M7: the legacy production dispatch-queue transport (its queue table,
# DB-backed WaveDispatcher seam factories, and queue-servicing wrapper). The
# deterministic host (``cli`` transport) replaced it. NOTE: the inter-agent
# message WIRE (``bridge_messages`` / ``bridge_send.py`` / ``bridge_read.py``)
# is a SEPARATE concern that STAYS — only the dispatch QUEUE was deleted.


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
        route subsequent spawns/sends. Called EXACTLY ONCE per cycle by the
        production binding (the deterministic host's ``CliDispatchTools``)."""
        ...

    def spawn_teammate(
        self, team_id: str, name: str, prompt: str, model: str | None = None
    ) -> None:
        """First-touch ``Agent`` spawn of teammate ``name`` INTO ``team_id``,
        run-in-background, fire-and-forget, with ``prompt`` as the full briefing.
        Required because CC does NOT auto-spawn on ``SendMessage`` (kaizen #59).

        ``model`` is the OPTIONAL per-task model-tier alias (``haiku`` |
        ``sonnet`` | ``opus``) chosen by ``scripts.model_tier`` (atelier
        model-tier selection). ``None`` (the default) inherits the session
        default — additive + back-compatible, so existing callers/impls are
        unbroken."""
        ...

    def send_message(self, team_id: str, to: str, message: str) -> None:
        """``SendMessage`` to an ALREADY-spawned teammate ``to`` in ``team_id``.
        Only valid once the teammate appears in the team's ``members[].name``
        (i.e. a prior :meth:`spawn_teammate` materialised it)."""
        ...

    def spawn_subagent(
        self, task_id: Any, attempt: int, prompt: str, model: str | None = None
    ) -> None:
        """Sub-agent mode: a fire-and-forget ``Agent`` (run-in-background) for
        ONE worker attempt. NOTHING team-related — no team, no membership, no
        first-touch. ``prompt`` is the full briefing.

        ``model`` is the OPTIONAL per-task model-tier alias (``haiku`` |
        ``sonnet`` | ``opus``); ``None`` (the default) inherits the session
        default — additive + back-compatible."""
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
    model: str | None = None,
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
    the briefing — the caller passes it pre-rendered so the mode-agnostic
    composer and the mode-specific dispatcher stay cleanly separated.

    ``model`` is the OPTIONAL per-task model-tier alias (``haiku`` | ``sonnet`` |
    ``opus``) selected by ``scripts.model_tier.recommend``. It is threaded into
    the spawn calls only; ``send_message`` is unchanged (the teammate's model was
    fixed at its first-touch spawn). ``None`` (the default) inherits the session
    default — additive + back-compatible.
    """
    if mode == DISPATCH_MODE_SUBAGENT:
        tools.spawn_subagent(task[_DISPATCH_TASK_ID_KEY], attempt, briefing, model=model)
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
            tools.spawn_teammate(team_id, teammate_name, briefing, model=model)
        return
    raise UnknownDispatchModeError(mode)


# ── _valid_positive_int_env — valid-or-ignore env-int parser ────────────────
#
# A shared helper kept here (re-exported to ``scripts.pm_dispatch``) for the
# valid-or-ignore env-var posture: a blank/garbage/negative value is IGNORED in
# favor of the default, never raised — a typo in an env var must not crash or
# wedge a cycle. (It was originally introduced alongside the now-removed
# bridge-queue tunables; pm_dispatch still imports it, so it stays.)


def _valid_positive_int_env(value: str | None, default: int) -> int:
    """Return ``int(value)`` iff it parses to a non-negative int, else ``default``.

    Mirrors :func:`scripts.model_tier._valid_tier`'s valid-or-ignore posture: a
    blank/garbage/negative env value is IGNORED (a typo never crashes a dispatch).
    """
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (ValueError, AttributeError):
        return default
    return parsed if parsed >= 0 else default


# Recover the worker's JSON envelope from a bridge_read fenced payload. The
# fence is ``<untrusted source="…" seq="…">{json}</untrusted>`` (bridge_read
# HTML-escapes the inner body with quote=False). We strip the wrapper + unescape
# the three element-content entities bridge_read._fence emits, then json.loads.
_FENCE_OPEN_RE = re.compile(r'^<untrusted source="[^"]*" seq="[^"]*">')
_FENCE_CLOSE = "</untrusted>"


def _parse_reply_envelope(payload: Any) -> Mapping[str, Any] | None:
    """Best-effort parse of a fenced bridge reply payload into a JSON Mapping.

    Returns ``None`` (never raises) on any shape that is not a parseable JSON
    object — the caller treats that as "no valid envelope here" and keeps
    scanning. Untrusted input: the payload is DATA; we only unescape + parse it.
    """
    if not isinstance(payload, str):
        return None
    body = payload
    m = _FENCE_OPEN_RE.match(body)
    if m is not None:
        # The payload was fenced by bridge_read._fence, which HTML-escapes the
        # element content with html.escape(quote=False) — only & < > transform.
        # Strip the wrapper, then reverse the escape in &amp;-LAST order so an
        # &amp;lt; in the source round-trips correctly. We unescape ONLY on the
        # fenced path: un-fenced raw JSON was never escaped, so unescaping it
        # would corrupt legitimate &amp; / &lt; / &gt; content in the envelope.
        body = body[m.end() :]
        if body.endswith(_FENCE_CLOSE):
            body = body[: -len(_FENCE_CLOSE)]
        body = body.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


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
