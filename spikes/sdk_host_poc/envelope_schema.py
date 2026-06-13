"""ENVELOPE_SCHEMA — JSON Schema fed to ``claude --json-schema`` (the agent's
forced structured output).

The real atelier validator is reused directly:

    from scripts.pm_dispatch_envelope import validate_envelope, EnvelopeValidationError

This works because:
* ``pm_dispatch_envelope.py`` is a pure, IO-free module (only reads RULES_SKILL
  at import time, no DB, no network).
* Its ``validate_envelope`` signature matches what we need:
      validate_envelope(envelope, *, dispatched_task_id, dispatched_attempt)
* It raises ``EnvelopeValidationError`` — a typed, named-field exception — on
  any structural or anti-spoof failure.
* It imports ``TERMINAL_STATUSES`` and ``RULES_SKILL`` from ``scripts.dispatch``
  (both available when running ``PYTHONPATH=.`` from the repo root).

DEVIATION NOTES
---------------
None.  The real atelier validator is used verbatim — this is the intended
KEEP-set plug-in (see design §4, ``pm_dispatch_envelope.py`` row, "ADAPT:
accept result.structured_output as input").

The ``ENVELOPE_SCHEMA`` below matches exactly the fields that
``validate_envelope`` checks: ``type``, ``task_id``, ``attempt``, ``status``,
``artifacts``, ``notes_md``.  In M0 it is threaded into the agent-call seam as
``SpikeOptions.output_format`` (see ``agent_call.run_attempt`` — the fake
records it and ``test_output_format_is_envelope_schema`` asserts the fake
received ``output_format is ENVELOPE_SCHEMA``).  In M3 the same object is passed
to the ``claude`` CLI as ``--json-schema`` so the agent emits a shape-valid
envelope (native structured output) before our post-call acceptance gate runs.
"""

from __future__ import annotations

# Re-export the real atelier validator and exception so callers import from
# this module only, keeping the spike self-contained.
from scripts.pm_dispatch_envelope import EnvelopeValidationError, validate_envelope

__all__ = ["ENVELOPE_SCHEMA", "EnvelopeValidationError", "validate_envelope"]

# JSON Schema threaded as SpikeOptions.output_format in M0 (and passed to
# ``claude --json-schema`` in M3).  Fields mirror the six checks in
# validate_envelope — in M3 the CLI emits native structured output conforming to
# this schema.
ENVELOPE_SCHEMA: dict = {
    "type": "object",
    "title": "TaskResultEnvelope",
    "description": (
        "Terminal task-result envelope.  The host validates this against the "
        "PM dispatch record (task_id + attempt) after every query()."
    ),
    "required": ["type", "task_id", "attempt", "status", "artifacts"],
    "additionalProperties": True,
    "properties": {
        "type": {
            "type": "string",
            "const": "task_result",
            "description": "Must be the literal string 'task_result'.",
        },
        "task_id": {
            "description": "Must match the task_id the host dispatched.",
            "oneOf": [{"type": "string"}, {"type": "integer"}],
        },
        "attempt": {
            "description": "Must match the attempt number the host dispatched.",
            "oneOf": [{"type": "integer"}, {"type": "string"}],
        },
        "status": {
            "type": "string",
            "enum": ["done", "blocked", "abandoned", "needs-input", "failed"],
            "description": "Terminal status from the four-token closure set.",
        },
        "artifacts": {
            "type": "array",
            "description": (
                "Non-empty list of deliverable artifacts unless status is "
                "'blocked', 'needs-input', or 'failed'."
            ),
            "items": {
                "type": "object",
                "description": "Legacy file artifact or first-class ref-stub artifact.",
            },
        },
        "notes_md": {
            "type": "string",
            "description": (
                "Markdown notes.  Required when status=='abandoned': "
                "first line MUST match ABANDON_RE."
            ),
        },
    },
}
