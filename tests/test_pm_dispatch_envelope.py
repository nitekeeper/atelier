"""AI-5 — pytest suite for `scripts/pm_dispatch_envelope.py` (atelier#60).

The envelope validator is a PURE layer: `validate_envelope(envelope, *,
dispatched_task_id, dispatched_attempt)` either returns a fresh validated dict
or raises `EnvelopeValidationError`. The only import-time IO is `ABANDON_RE`,
which is single-sourced from `internal/team-mode-rules/SKILL.md` — these tests
bind that fence (a SKILL edit that breaks the grammar fails CI).

Matrix covered here:

* `ABANDON_RE` compiles AND matches a known-good abandon line; the pattern is
  non-trivial (binds the SKILL fence — every documented category is present).
* Each bad envelope shape is rejected with a NAMED field diagnostic:
    - wrong `type` (!= task_result)
    - `status` not in TERMINAL_STATUSES
    - non-empty-artifacts rule (empty allowed ONLY for blocked/needs-input;
      empty-on-done rejected)
    - `abandoned` with notes_md line-1 NOT matching ABANDON_RE rejected
* Anti-spoof: task_id mismatch vs dispatched_task_id rejected; attempt mismatch
  vs dispatched_attempt rejected.
* Fail-closed: a validation failure NEVER returns a 'done' result (it raises).
"""

from __future__ import annotations

import pytest

from scripts.dispatch import TERMINAL_STATUSES
from scripts.pm_dispatch_envelope import (
    ABANDON_RE,
    EnvelopeValidationError,
    validate_envelope,
)

# Every category the rules SKILL abandon-grammar fence enumerates. If the SKILL
# fence is edited to drop/rename one, the binding test below fails — that is the
# point (the grammar is single-sourced, never re-typed as a Python literal).
_DOCUMENTED_ABANDON_CATEGORIES = [
    "scope",
    "blocked",
    "conflict",
    "capacity",
    "stale_rules",
    "no_consensus",
    "destructive_rejected",
    "tests_unrecoverable",
]


def _good_envelope(**overrides) -> dict:
    """A minimal VALID `done` envelope for task 7 / attempt 1. Overrides let a
    test mutate exactly one field so the assertion isolates that field."""
    env = {
        "type": "task_result",
        "task_id": 7,
        "attempt": 1,
        "status": "done",
        "artifacts": [{"path": "scripts/foo.py", "sha": "abc123"}],
        "notes_md": "Implemented foo.",
        "next_action": "review",
    }
    env.update(overrides)
    return env


# ── ABANDON_RE binds the SKILL fence ────────────────────────────────────────


def test_abandon_re_compiles_and_matches_known_good_line():
    """The single-sourced grammar matches a canonical abandon first line and
    captures the category group."""
    line = "ABANDON: capacity:5-attempt budget exhausted without convergence"
    m = ABANDON_RE.match(line)
    assert m is not None
    assert m.group("category") == "capacity"
    assert "exhausted" in m.group("reason")


def test_abandon_re_is_non_trivial_binds_skill_fence():
    """The pattern is non-trivial: it is anchored, has both named groups, and
    enumerates EVERY documented category. This binds the SKILL.md fence — a
    fence edit that drops a category (or weakens the grammar to `.*`) fails
    here rather than silently shipping a stale/permissive regex."""
    pat = ABANDON_RE.pattern
    # Anchored start/end — not a substring-permissive pattern.
    assert pat.startswith("^ABANDON: (?P<category>")
    assert pat.endswith("$")
    # Both named capture groups present.
    assert "(?P<category>" in pat
    assert "(?P<reason>" in pat
    # Every documented category token is in the alternation.
    for category in _DOCUMENTED_ABANDON_CATEGORIES:
        assert category in pat, f"abandon category {category!r} missing from ABANDON_RE"


def test_abandon_re_rejects_unknown_category():
    """A category outside the closed enum does not match (the alternation is a
    closed set, not `.*`)."""
    assert ABANDON_RE.match("ABANDON: made_up_category:some reason") is None


def test_abandon_re_rejects_missing_reason():
    """`{1,200}` on the reason means an empty reason fails to match."""
    assert ABANDON_RE.match("ABANDON: scope:") is None


# ── Happy path ──────────────────────────────────────────────────────────────


def test_valid_done_envelope_returns_fresh_dict():
    env = _good_envelope()
    result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result["status"] == "done"
    assert result["task_id"] == 7
    # Fresh dict — caller cannot alias PM-internal state.
    assert result is not env
    result["status"] = "mutated"
    assert env["status"] == "done"


def test_valid_abandoned_envelope_with_good_abandon_line():
    env = _good_envelope(
        status="abandoned",
        notes_md="ABANDON: capacity:budget exhausted\n\nmore detail follows",
    )
    result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result["status"] == "abandoned"


def test_stringified_task_id_and_attempt_accepted():
    """The SKILL permits a bare int OR stringified task_id/attempt; they are
    string-normalized before compare."""
    env = _good_envelope(task_id="7", attempt="1")
    result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result["task_id"] == "7"


# ── Bad shape: type ─────────────────────────────────────────────────────────


def test_rejects_wrong_type():
    env = _good_envelope(type="status_update")
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "type"
    assert exc.value.expected == "task_result"
    assert exc.value.got == "status_update"


def test_rejects_non_mapping_envelope():
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(["not", "a", "mapping"], dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "envelope"


# ── Bad shape: status ───────────────────────────────────────────────────────


def test_rejects_status_not_in_terminal_statuses():
    env = _good_envelope(status="in-progress")
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "status"
    assert exc.value.got == "in-progress"
    # The diagnostic surfaces the allowed set.
    assert set(exc.value.expected) == set(TERMINAL_STATUSES)


# ── Bad shape: artifacts (non-empty rule) ───────────────────────────────────


def test_rejects_empty_artifacts_on_done():
    """Empty artifacts allowed ONLY for blocked/needs-input — empty-on-done
    rejected."""
    env = _good_envelope(status="done", artifacts=[])
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "artifacts"


def test_rejects_non_list_artifacts():
    env = _good_envelope(artifacts="scripts/foo.py")
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "artifacts"


@pytest.mark.parametrize("status", ["blocked", "needs-input"])
def test_empty_artifacts_allowed_for_blocked_and_needs_input(status):
    """Empty artifacts is explicitly OK for the two non-terminal statuses."""
    env = _good_envelope(status=status, artifacts=[])
    result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result["status"] == status


def test_non_empty_artifacts_required_only_for_done_and_abandoned():
    """A `done` envelope with non-empty artifacts passes the artifacts gate."""
    env = _good_envelope(status="done", artifacts=[{"path": "a", "sha": "b"}])
    result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result["status"] == "done"


# ── Bad shape: abandoned notes_md grammar ───────────────────────────────────


def test_rejects_abandoned_with_bad_abandon_line():
    """`abandoned` whose notes_md line-1 does not match ABANDON_RE is rejected
    with a notes_md-named diagnostic."""
    env = _good_envelope(
        status="abandoned",
        notes_md="I gave up because it was hard",
    )
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "notes_md"


def test_rejects_abandoned_with_non_string_notes_md():
    env = _good_envelope(status="abandoned", notes_md=None)
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "notes_md"


def test_rejects_abandoned_when_abandon_line_not_on_first_line():
    """The grammar binds LINE 1 specifically; a valid abandon line buried on
    line 2 does not satisfy the contract."""
    env = _good_envelope(
        status="abandoned",
        notes_md="some preamble\nABANDON: scope:out of scope",
    )
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "notes_md"


# ── Anti-spoof: identity binding ────────────────────────────────────────────


def test_rejects_task_id_mismatch():
    """A worker cannot close a task it was not dispatched against."""
    env = _good_envelope(task_id=999)
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "task_id"
    assert exc.value.got == 999


def test_rejects_missing_task_id():
    env = _good_envelope()
    del env["task_id"]
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "task_id"


def test_rejects_attempt_mismatch():
    """A worker cannot launder an old attempt's reply as the current one."""
    env = _good_envelope(attempt=4)
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "attempt"
    assert exc.value.got == 4


def test_rejects_missing_attempt():
    env = _good_envelope()
    del env["attempt"]
    with pytest.raises(EnvelopeValidationError) as exc:
        validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert exc.value.field == "attempt"


# ── Fail-closed: a validation failure never yields a 'done' result ──────────


@pytest.mark.parametrize(
    "env",
    [
        _good_envelope(type="garbage"),
        _good_envelope(status="in-progress"),
        _good_envelope(status="done", artifacts=[]),
        _good_envelope(task_id=999),
        _good_envelope(attempt=99),
        _good_envelope(status="abandoned", notes_md="no grammar here"),
    ],
)
def test_fail_closed_invalid_envelope_never_returns_done(env):
    """Every malformed envelope raises rather than being coerced to a closure.
    There is no return-path that yields a 'done' (or any) dict on failure."""
    result = None
    with pytest.raises(EnvelopeValidationError):
        result = validate_envelope(env, dispatched_task_id=7, dispatched_attempt=1)
    assert result is None
