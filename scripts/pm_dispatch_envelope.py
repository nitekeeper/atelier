# scripts/pm_dispatch_envelope.py
"""Atelier team-mode PM-side reply-envelope validation — a PURE layer.

This module is AI-2 of the team-mode cycle. It validates a worker's terminal
reply envelope (the only acceptable terminal shape, defined in
``internal/team-mode-rules/SKILL.md`` "Reply envelope") BEFORE the PM
scheduler / cycle loop is allowed to treat the attempt as closed.

Design constraints (deliberate, load-bearing):

* **Pure.** No DB, no IO at call time, no clock. The only IO is at *import*
  time — the abandon-grammar regex is single-sourced from the rules SKILL
  (see :data:`ABANDON_RE` below) so the markdown stays canonical and a SKILL
  edit that breaks the fence fails CI. ``validate_envelope`` itself is a pure
  ``dict -> dict`` (or raise) function, unit-testable in isolation.

* **Single-source the grammar.** :data:`ABANDON_RE` is NOT a re-typed Python
  literal. At import time we read ``internal/team-mode-rules/SKILL.md``, locate
  the abandon-grammar regex line (anchored on ``^ABANDON: (?P<category>``),
  and compile that string ONCE. If the fence cannot be found we raise loudly,
  naming the SKILL path — so a markdown edit that moves/breaks the grammar
  fails at import (CI) rather than silently shipping a stale regex.

* **Single-source the status tokens.** :data:`TERMINAL_STATUSES` is imported
  verbatim from :mod:`scripts.dispatch` — zero re-typed status literals.
  Terminal-ONLY gating (``done``/``abandoned``) is NOT this file's job; that
  lives in the PM loop (AI-3). This file only rejects statuses outside the
  four-token closure set.

* **Fail-closed, named-field diagnostics.** Every check raises a typed
  :class:`EnvelopeValidationError` carrying the offending field name plus the
  expected/got values — mirroring :class:`scripts.dispatch.MissingRenderVarsError`'s
  sorted-names diagnostic style. A malformed envelope is NEVER coerced to a
  ``done``/``abandoned`` outcome; the caller treats a validation failure as a
  failed attempt.

* **Untrusted content.** Envelope content is untrusted DATA. We compare,
  pattern-match, and echo it in diagnostics — never ``eval``/``exec`` it and
  never interpolate it into anything executable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Reuse the canonical four-token closure set — do NOT re-type status literals.
from scripts.dispatch import RULES_SKILL, TERMINAL_STATUSES

# ── Single-sourced abandon grammar ─────────────────────────────────────────

# Statuses for which an empty artifacts list is acceptable per the rules SKILL
# ("Empty array allowed only for `blocked`/`needs-input`.") plus the terminal
# `failed` (a hard run-and-failed signal — like `abandoned`, a failure may have
# produced no artifacts, so an empty list is acceptable).
_ARTIFACTS_OPTIONAL_STATUSES: frozenset[str] = frozenset({"blocked", "needs-input", "failed"})

# ── Reference-stub artifact (cycle-1 payload referencing) ──────────────────
#
# An artifact entry may be EITHER the legacy file shape
# (``{"path": ..., "sha": ...}``) OR a first-class *reference-stub* shape that
# points at an out-of-band body persisted in the content-addressed
# ``bridge_payloads`` store (``scripts/bridge_payloads.py``) — the same sha256
# bridge_send records in ``bridge_messages.payload_ref``. The ref shape lets a
# worker close a task whose deliverable is a large referenced body WITHOUT
# inlining that body into the envelope.
#
# Recognition is PURELY STRUCTURAL — :func:`is_reference_artifact` checks shape
# only and NEVER dereferences the store (validate_envelope stays a pure,
# offline ``dict -> dict``). A declared-but-malformed ref (``kind == "ref"`` yet
# no content address) is rejected so a dangling/hollow ref cannot satisfy the
# artifacts-non-empty contract and hide hollow work; the *liveness* of the ref
# (is the sha actually present in the store?) is verified later, at resolve
# time, by bridge_read — never here.
_REF_ARTIFACT_KIND = "ref"


def _declares_ref_kind(artifact: Any) -> bool:
    """True iff ``artifact`` is a mapping that DECLARES itself a ref stub.

    Declaration is the ``kind == "ref"`` discriminator alone — it says nothing
    about whether the ref is well-formed (see :func:`is_reference_artifact`).
    """
    return isinstance(artifact, Mapping) and artifact.get("kind") == _REF_ARTIFACT_KIND


def is_reference_artifact(artifact: Any) -> bool:
    """Return whether ``artifact`` is a WELL-FORMED first-class ref-stub artifact.

    A well-formed ref artifact is a mapping with ``kind == "ref"`` AND a present,
    non-empty string ``sha256`` content address (the coordinate
    ``scripts.bridge_payloads`` keys a body by, mirrored in
    ``bridge_messages.payload_ref``). Optional companion fields (``tag``,
    ``bytes``/``byte_len``, ``team_id``) are accepted permissively and not
    required.

    STRUCTURAL ONLY: this never opens the store / dereferences the sha — a ref
    can be well-formed here yet dangling in the store, which resolve-time
    (bridge_read) catches fail-closed. Keeping it pure preserves the
    offline-``dict -> dict`` contract of :func:`validate_envelope`.
    """
    if not _declares_ref_kind(artifact):
        return False
    sha256 = artifact.get("sha256")
    return isinstance(sha256, str) and bool(sha256.strip())


# Anchor for the abandon-grammar regex line inside the rules SKILL fenced block.
# The line IS the regex (see SKILL "Abandon grammar (regex)").
_ABANDON_ANCHOR = "^ABANDON: (?P<category>"


def _extract_abandon_regex(skill_text: str) -> str:
    """Locate + return the abandon-grammar regex string from the rules SKILL.

    The regex lives on a single line inside a fenced code block, beginning with
    ``^ABANDON: (?P<category>`` (see ``internal/team-mode-rules/SKILL.md`` →
    "Abandon grammar (regex)"). We return that line verbatim (stripped of
    surrounding whitespace) so the markdown stays the single source of truth.

    Raises :class:`RuntimeError` naming the SKILL path if no such line exists —
    fail loud so a SKILL edit that breaks the fence fails CI rather than
    silently shipping a stale/empty grammar.
    """
    for raw_line in skill_text.splitlines():
        line = raw_line.strip()
        if line.startswith(_ABANDON_ANCHOR):
            return line
    raise RuntimeError(
        f"abandon-grammar regex not found in {RULES_SKILL}: no line beginning "
        f"{_ABANDON_ANCHOR!r} inside a fenced code block. The rules SKILL is the "
        "single source of truth for ABANDON_RE; a fence edit that breaks this "
        "anchor MUST fail CI."
    )


def _load_abandon_re() -> re.Pattern[str]:
    """Read the rules SKILL at import time + compile the abandon grammar once."""
    try:
        skill_text = RULES_SKILL.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover — environment breakage
        raise RuntimeError(
            f"team-mode rules SKILL.md not found at {RULES_SKILL}; cannot derive "
            "ABANDON_RE (the grammar is single-sourced from the SKILL, never "
            "re-typed in Python)"
        ) from exc
    return re.compile(_extract_abandon_regex(skill_text))


# Compiled ONCE at import — single-sourced from the rules SKILL markdown.
ABANDON_RE: re.Pattern[str] = _load_abandon_re()


# ── Exceptions ─────────────────────────────────────────────────────────────


class EnvelopeValidationError(ValueError):
    """Raised by :func:`validate_envelope` when a worker reply envelope fails a
    fail-closed contract check.

    Carries the offending ``field`` name plus the ``expected`` and ``got``
    values so the PM diagnostic surfaces actionable info (named field, not a
    generic message) — mirroring
    :class:`scripts.dispatch.MissingRenderVarsError`'s style. A validation
    failure means the caller MUST treat the attempt as failed; the envelope is
    NEVER coerced to a ``done``/``abandoned`` outcome.
    """

    def __init__(self, field: str, expected: Any, got: Any) -> None:
        self.field = field
        self.expected = expected
        self.got = got
        super().__init__(
            f"envelope field {field!r} failed validation: expected {expected!r}, got {got!r}"
        )


# ── Envelope validation (pure) ─────────────────────────────────────────────


def _present(envelope: Mapping[str, Any], key: str) -> bool:
    """A field is "present" iff the key exists and its value is not ``None``."""
    return key in envelope and envelope.get(key) is not None


def validate_envelope(
    envelope: Mapping[str, Any],
    *,
    dispatched_task_id: str | int,
    dispatched_attempt: int,
) -> dict[str, Any]:
    """Validate a worker's terminal reply envelope, fail-closed.

    ``dispatched_task_id`` / ``dispatched_attempt`` are keyword-only by design:
    they come from the PM's own dispatch record, NOT from the (untrusted)
    envelope, so a worker cannot spoof them positionally. The envelope's
    ``task_id`` / ``attempt`` MUST match these to reject cross-task spoofing and
    attempt-laundering.

    Checks (each raises :class:`EnvelopeValidationError` naming the field):

    1. ``type == "task_result"``
    2. ``task_id`` present AND equals ``dispatched_task_id`` (string-normalized,
       since the SKILL permits a bare-int or stringified task_id)
    3. ``attempt`` present AND equals ``dispatched_attempt`` (string-normalized)
    4. ``status`` in :data:`TERMINAL_STATUSES` (rejects unknown/extra tokens)
    5. ``artifacts`` is a list; non-empty UNLESS ``status`` is ``blocked`` or
       ``needs-input``. Each entry may be a legacy file artifact
       (``{"path", "sha"}``) OR a first-class reference-stub artifact
       (``{"kind": "ref", "sha256": ...}`` — see :func:`is_reference_artifact`),
       which counts toward non-empty WITHOUT inlining the referenced body. An
       entry that DECLARES ``kind == "ref"`` but lacks a ``sha256`` content
       address is rejected (a dangling/hollow ref must not mask hollow work);
       the ref is NEVER dereferenced here (resolve-time verifies liveness).
    6. when ``status == "abandoned"``: line 1 of ``notes_md`` matches
       :data:`ABANDON_RE` (the offending line is echoed in the diagnostic)

    Returns a shallow copy of the validated envelope (a fresh ``dict`` so the
    caller cannot alias PM-internal state). NOTE: the copy is SHALLOW — nested
    values (``artifacts`` list entries, ``notes_md``) remain aliased to the
    caller's input, so a deep mutation of those still reflects in the original.
    NEVER coerces status. Envelope content is untrusted DATA — it is only
    compared / pattern-matched / echoed, never executed or interpolated into
    anything executable.
    """
    if not isinstance(envelope, Mapping):
        raise EnvelopeValidationError(
            field="envelope", expected="a JSON object (mapping)", got=type(envelope).__name__
        )

    # 1. type discriminator
    env_type = envelope.get("type")
    if env_type != "task_result":
        raise EnvelopeValidationError(field="type", expected="task_result", got=env_type)

    # 2. task_id present + matches dispatch record (anti cross-task spoof).
    if not _present(envelope, "task_id"):
        raise EnvelopeValidationError(
            field="task_id", expected=f"present and == {dispatched_task_id!r}", got=None
        )
    env_task_id = envelope["task_id"]
    # SKILL permits a bare-int OR stringified task_id — normalize before compare.
    if str(env_task_id) != str(dispatched_task_id):
        raise EnvelopeValidationError(field="task_id", expected=dispatched_task_id, got=env_task_id)

    # 3. attempt present + matches dispatch record (anti attempt-laundering).
    if not _present(envelope, "attempt"):
        raise EnvelopeValidationError(
            field="attempt", expected=f"present and == {dispatched_attempt!r}", got=None
        )
    env_attempt = envelope["attempt"]
    if str(env_attempt) != str(dispatched_attempt):
        raise EnvelopeValidationError(field="attempt", expected=dispatched_attempt, got=env_attempt)

    # 4. status in the four-token closure set (terminal-only gating is AI-3's job).
    status = envelope.get("status")
    if status not in TERMINAL_STATUSES:
        raise EnvelopeValidationError(
            field="status", expected=sorted(TERMINAL_STATUSES), got=status
        )

    # 5. artifacts must be a list; non-empty unless blocked/needs-input.
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, list):
        raise EnvelopeValidationError(
            field="artifacts",
            expected="a list",
            got=type(artifacts).__name__ if artifacts is not None else None,
        )
    if not artifacts and status not in _ARTIFACTS_OPTIONAL_STATUSES:
        raise EnvelopeValidationError(
            field="artifacts",
            expected=f"non-empty list (empty allowed only for {sorted(_ARTIFACTS_OPTIONAL_STATUSES)})",
            got=artifacts,
        )
    # First-class ref-stub artifacts (cycle-1 payload referencing): a ref
    # artifact is permissively accepted and COUNTS toward artifacts-non-empty
    # WITHOUT carrying inline content. The only added teeth are structural —
    # an entry that DECLARES itself a ref (``kind == "ref"``) MUST be
    # well-formed (a present sha256 content address); a dangling/hollow ref is
    # rejected so it cannot silently satisfy the non-empty contract and mask
    # hollow work. Non-ref shapes (legacy ``{path, sha}``, etc.) are untouched —
    # the acceptor branches on shape and adds NO new rejection for them. The
    # check is structural only; the store is NEVER dereferenced here (resolve
    # time / bridge_read verifies liveness fail-closed).
    for index, artifact in enumerate(artifacts):
        if _declares_ref_kind(artifact) and not is_reference_artifact(artifact):
            raise EnvelopeValidationError(
                field="artifacts",
                expected=(
                    f"artifacts[{index}] declares kind=='{_REF_ARTIFACT_KIND}' so it MUST carry a "
                    "non-empty 'sha256' content address (structural; not dereferenced here)"
                ),
                got=artifact,
            )

    # 6. abandoned → notes_md line 1 must match the single-sourced grammar.
    if status == "abandoned":
        notes_md = envelope.get("notes_md")
        if not isinstance(notes_md, str):
            raise EnvelopeValidationError(
                field="notes_md",
                expected="a string whose first line matches ABANDON_RE",
                got=type(notes_md).__name__ if notes_md is not None else None,
            )
        first_line = notes_md.splitlines()[0] if notes_md else ""
        if ABANDON_RE.match(first_line) is None:
            raise EnvelopeValidationError(
                field="notes_md",
                expected=f"first line matching {ABANDON_RE.pattern!r}",
                got=first_line,
            )

    # Validated — return a fresh dict (no aliasing of PM-internal state).
    return dict(envelope)
