"""Tests for scripts/envelope_schema.py — the CLI --json-schema envelope schema.

The schema is the SHAPE constraint passed to ``claude --json-schema``; the
load-bearing semantic + anti-spoof checks stay in ``validate_envelope``. These
tests pin the schema's agreement with the validator's field set + closure tokens
so the two cannot drift.
"""

from __future__ import annotations

import json

from scripts.dispatch import TERMINAL_STATUSES
from scripts.envelope_schema import ENVELOPE_SCHEMA


def test_schema_required_fields_match_validator_checks():
    """The required fields are exactly the ones validate_envelope inspects."""
    assert set(ENVELOPE_SCHEMA["required"]) == {
        "type",
        "task_id",
        "attempt",
        "status",
        "artifacts",
        "notes_md",
    }


def test_status_enum_matches_terminal_statuses():
    """The status enum is BUILT from TERMINAL_STATUSES (not re-typed) — a change
    to the closure set flows through. Sorted for byte-stability."""
    status_schema = ENVELOPE_SCHEMA["properties"]["status"]
    assert set(status_schema["enum"]) == set(TERMINAL_STATUSES)
    assert status_schema["enum"] == sorted(TERMINAL_STATUSES)


def test_type_is_const_task_result():
    assert ENVELOPE_SCHEMA["properties"]["type"]["const"] == "task_result"


def test_schema_is_json_serialisable_and_stable():
    """The schema serialises cleanly (it is passed as a --json-schema argv item)
    and is byte-stable across calls (deterministic dispatch argv)."""
    a = json.dumps(ENVELOPE_SCHEMA, sort_keys=True)
    b = json.dumps(ENVELOPE_SCHEMA, sort_keys=True)
    assert a == b
    # task_id is a SINGLE type "string" — claude's --json-schema (ajv strictTypes)
    # rejects union types; the worker always echoes the string id we hand it.
    assert ENVELOPE_SCHEMA["properties"]["task_id"]["type"] == "string"
