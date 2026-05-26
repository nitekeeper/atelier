"""Tests for ``scripts.dispatch.WaveTracker``.

WaveTracker is the in-process bookkeeper for which expected
participants of a wave have reported a terminal envelope status
(``done|blocked|abandoned|needs-input``, per TM-006). Foundational
scaffolding — the wave-5 scheduler will replace it with a DB-backed
read of the durable task_results table, but the public surface tested
here MUST remain stable so call sites do not have to change.
"""

from __future__ import annotations

import pytest

from scripts.dispatch import TERMINAL_STATUSES, WaveTracker


def _new_tracker() -> WaveTracker:
    """Fresh tracker for two expected teammates — used as the common
    fixture so each test exercises a clean, isolated state machine."""
    return WaveTracker(wave_id="wave-3", expected={"backend-engineer-1", "sdet-1"})


def test_record_accepts_terminal_status() -> None:
    """The four TM-006 closure tokens must all be acceptable inputs."""
    tracker = _new_tracker()
    for status in sorted(TERMINAL_STATUSES):
        # Reuse the same role_id; we are exercising the status validator,
        # not multi-member bookkeeping (covered separately below).
        tracker.record("backend-engineer-1", status)
    # Last write wins — assert the final status is whatever sort order yielded.
    assert tracker.reports["backend-engineer-1"] in TERMINAL_STATUSES


def test_record_bad_status_raises_value_error() -> None:
    """A typo or junk status must raise ``ValueError`` synchronously —
    silent acceptance would mask a contract violation and only surface
    when the scheduler later tried to gate the wave."""
    tracker = _new_tracker()
    with pytest.raises(ValueError) as excinfo:
        tracker.record("backend-engineer-1", "complete")  # not in TM-006
    # Diagnostic surface: the message should at least mention the bad token.
    assert "complete" in str(excinfo.value)


def test_outstanding_returns_only_unreported_members() -> None:
    """``outstanding()`` is the set difference between expected and
    reported. A member who has reported ANY status (terminal-only or
    not) is no longer outstanding."""
    tracker = _new_tracker()
    assert tracker.outstanding() == {"backend-engineer-1", "sdet-1"}
    tracker.record("backend-engineer-1", "done")
    assert tracker.outstanding() == {"sdet-1"}
    tracker.record("sdet-1", "blocked")
    assert tracker.outstanding() == set()


def test_is_complete_requires_all_expected_to_report() -> None:
    """``is_complete()`` is True iff every expected member has recorded
    any status. Non-terminal statuses still count for completeness —
    ``terminal_only()`` is the stricter gate."""
    tracker = _new_tracker()
    assert tracker.is_complete() is False
    tracker.record("backend-engineer-1", "done")
    assert tracker.is_complete() is False
    tracker.record("sdet-1", "needs-input")
    assert tracker.is_complete() is True


def test_terminal_only_requires_done_or_abandoned() -> None:
    """``terminal_only()`` only returns True when every expected member
    reported either ``done`` or ``abandoned``. ``blocked`` or
    ``needs-input`` mean the wave is not yet ready to close — the
    scheduler must re-dispatch or answer first."""
    tracker = _new_tracker()
    tracker.record("backend-engineer-1", "done")
    tracker.record("sdet-1", "blocked")
    # All reported (so is_complete True) but blocked is non-terminal-only.
    assert tracker.is_complete() is True
    assert tracker.terminal_only() is False

    # Move sdet-1 to abandoned — now terminal_only flips True.
    tracker.record("sdet-1", "abandoned")
    assert tracker.terminal_only() is True


def test_summary_emits_serialisable_snapshot() -> None:
    """``summary()`` returns a JSON-serialisable dict suitable for log
    emission. Keys are stable; lists are sorted for deterministic
    diffability across runs."""
    tracker = _new_tracker()
    tracker.record("backend-engineer-1", "done")

    snapshot = tracker.summary()
    assert snapshot["wave_id"] == "wave-3"
    assert snapshot["expected"] == ["backend-engineer-1", "sdet-1"]  # sorted
    assert snapshot["reports"] == {"backend-engineer-1": "done"}
    assert snapshot["outstanding"] == ["sdet-1"]
    assert snapshot["complete"] is False
    assert snapshot["terminal_only"] is False

    # The snapshot must round-trip through json.dumps without raising —
    # that is the actual contract its consumers will exercise.
    import json

    json.dumps(snapshot)
