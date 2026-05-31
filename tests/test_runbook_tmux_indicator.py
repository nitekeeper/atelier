"""Presence + content gate for the #79 tmux agent-state runbook (atelier#79).

Non-vacuous: these assertions fail if the runbook is deleted or its load-bearing
facts (the hook-event map, the no-allow-passthrough correction, the live-
coexistence caveat, the 3-state legend, both operator install paths) are removed.
"""

from pathlib import Path

RUNBOOK = (
    Path(__file__).resolve().parent.parent / "docs" / "runbooks" / "tmux-agent-state-indicator.md"
)


def test_runbook_exists():
    assert RUNBOOK.is_file(), f"missing #79 runbook at {RUNBOOK}"


def test_runbook_documents_operator_install_paths():
    text = RUNBOOK.read_text(encoding="utf-8")
    # curl|bash path AND the TPM path.
    assert "install.sh" in text
    assert "@plugin 'accessd/tmux-agent-indicator'" in text


def test_runbook_documents_hook_event_state_map():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "UserPromptSubmit" in text
    assert "PermissionRequest" in text
    assert "Stop" in text
    # The correction: the event is PermissionRequest, NOT Notification.
    assert "not `Notification`" in text or "not Notification" in text


def test_runbook_documents_no_allow_passthrough_correction():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "allow-passthrough" in text
    assert "NOT required" in text


def test_runbook_documents_needs_testing_coexistence_caveat():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "NEEDS-TESTING" in text


def test_runbook_documents_three_state_legend():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "running" in text
    assert "needs-input" in text
    assert "done" in text
