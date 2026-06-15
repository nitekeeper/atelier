"""ENVELOPE_SCHEMA — the JSON Schema for a worker's terminal reply envelope.

This is the schema passed to ``claude -p ... --json-schema '<ENVELOPE_SCHEMA>'``
so the CLI emits a *shape-valid* ``structured_output`` object by construction
(constrained decoding). It is the schema-first analog of the bridge transport's
free-text reply envelope.

**Single-sourced against the validator.** The fields and the four-token closure
set are derived directly from what
:func:`scripts.pm_dispatch_envelope.validate_envelope` checks:

* ``type`` — fixed ``"task_result"`` discriminator (validator check 1).
* ``task_id`` — string (validator check 2; string-normalized compare). A single
  type, NOT a union: claude's ``--json-schema`` (ajv ``strictTypes``) rejects
  union types, and a worker always echoes the string task_id we hand it.
* ``attempt`` — integer (validator check 3; string-normalized compare).
* ``status`` — one of :data:`scripts.dispatch.TERMINAL_STATUSES` (validator
  check 4). The enum is BUILT from that frozenset (sorted for determinism), NOT
  re-typed, so a SKILL/constant change to the closure set flows through here and
  the test ``test_envelope_schema_status_enum_matches_terminal_statuses`` pins
  the agreement.
* ``artifacts`` — an array (validator check 5; the non-empty / ref-stub rules are
  post-decode validator concerns, NOT expressible as a hard schema constraint
  here because empty IS legal for ``blocked``/``needs-input``/``failed``).
* ``notes_md`` — a string (validator check 6 inspects its first line for the
  abandon grammar when ``status == "abandoned"``).

The schema is deliberately PERMISSIVE relative to ``validate_envelope``: the CLI
``--json-schema`` constrains the *shape*, and ``validate_envelope`` remains the
fail-closed, anti-spoof acceptance gate that runs against the host's OWN dispatch
identity (``dispatched_task_id`` / ``dispatched_attempt``). We never trust the
model's self-reported ``task_id`` / ``attempt`` even though the schema requires
them — they are re-checked against the journal dispatch row downstream. The
schema cannot enforce the cross-field "artifacts non-empty unless
blocked/needs-input/failed" rule or the abandon-grammar first-line rule, so those
stay in the validator (the schema does the easy shape work; the validator does
the load-bearing semantic + anti-spoof work).

``additionalProperties`` is left permissive (workers append e.g. ``next_action``)
— matching the validator, which ignores unknown keys and only checks the fields
above.
"""

from __future__ import annotations

from typing import Any

from scripts.dispatch import TERMINAL_STATUSES

#: The terminal-envelope JSON Schema. Built from ``TERMINAL_STATUSES`` (sorted so
#: the rendered schema is byte-stable across runs — load-bearing for the
#: ResultJournal key, which hashes the briefing but NOT the schema; still, a
#: stable schema keeps the dispatched argv deterministic for the argv-equality
#: tests).
ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "const": "task_result"},
        # task_id is a string: claude's --json-schema (ajv strictTypes) REJECTS
        # union types, so a single type is required. A worker echoes the task_id
        # we hand it (always a string), and validate_envelope string-normalizes
        # before comparing, so string-only stays consistent with the validator.
        "task_id": {"type": "string"},
        "attempt": {"type": "integer"},
        "status": {"type": "string", "enum": sorted(TERMINAL_STATUSES)},
        "artifacts": {"type": "array", "items": {"type": "object"}},
        "notes_md": {"type": "string"},
    },
    "required": ["type", "task_id", "attempt", "status", "artifacts", "notes_md"],
    # Permissive on extras to match validate_envelope (e.g. next_action). The
    # validator ignores unknown keys; the schema mirrors that.
    "additionalProperties": True,
}


__all__ = ["ENVELOPE_SCHEMA"]
