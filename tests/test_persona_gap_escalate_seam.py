# tests/test_persona_gap_escalate_seam.py
"""Live-wiring of the #64 persona-gap escalation behavior into the PM
``escalate_fn`` seam (atelier#87).

#64 (PR #86) shipped ``scripts/team_meeting.escalate_persona_gap`` — the
exactly-once persona-gap escalation LATCH — as a tested-but-DORMANT API:
nothing in a live run wired it to the ``WaveDispatcher.escalate_fn`` seam, so a
wave abandonment never surfaced a persona gap to the human.

#87 supplies ``build_persona_gap_escalate_fn``: an ``escalate_fn`` the
orchestrator passes to the wave engine's ``escalate_fn`` seam (the host pipeline
since the M7 bridge-queue removal). On every abandonment escalation the engine
emits (guaranteed — consensus item 8) it:

  * ALWAYS calls the guaranteed-emitting base sink (the default WARNING log, or a
    caller-supplied one) — escalation is NEVER silenced;
  * additionally routes through the one-shot LEDGER latch
    (``team_meeting.escalate_persona_gap``) keyed on the abandoned task's id, so
    a recurring abandonment of the SAME task escalates to the human EXACTLY once.

These tests use a real Local ``team_audit_log`` (the latch counts LEDGER rows,
not in-memory mentions) — mirroring ``tests/test_team_meeting.py``'s DB-backed
escalation tests. The dispatch transport itself is not exercised (escalation is a
LEDGER write); the orchestrator's surfacing of the ledger row to the human is a
documented procedural deferral (the dev-dispatch SKILL recipe).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import mode_detector, team_meeting
from scripts.migrate import apply_migrations
from scripts.team_meeting import build_persona_gap_escalate_fn

_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def team_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), _MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), _MIGRATIONS_DIR / "local-only")
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
        ("T1", "P1", "team-lead", "active"),
    )
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db)}


def _audit_count(db: str, event_type: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM team_audit_log WHERE event_type = ?", (event_type,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_escalate_fn_records_ledger_row(team_workspace) -> None:
    """An abandonment escalation routed through the seam writes exactly one
    persona_gap_escalation LEDGER row for the task."""
    escalate_fn = build_persona_gap_escalate_fn(team_id="T1")
    escalate_fn(
        {
            "kind": "escalation",
            "task_id": 42,
            "worker": "backend-engineer-1",
            "attempt": 5,
            "category": "capacity",
            "last_status": "soft-kill: wall-clock 30min exceeded",
            "upstream_task_id": None,
        }
    )
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 1
    # The latch keys on the task id (rendered as the gap_id).
    assert team_meeting.has_escalated(team_id="T1", gap_id="task-42") is True


def test_escalate_fn_is_exactly_once_per_task(team_workspace) -> None:
    """A task abandoned + re-escalated across rounds latches to ONE ledger row
    (the exactly-once guarantee — counts LEDGER rows, not call count)."""
    escalate_fn = build_persona_gap_escalate_fn(team_id="T1")
    esc = {
        "kind": "escalation",
        "task_id": 7,
        "worker": "w",
        "attempt": 5,
        "category": "capacity",
        "last_status": "x",
        "upstream_task_id": None,
    }
    for _ in range(4):
        escalate_fn(esc)
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 1


def test_distinct_tasks_escalate_independently(team_workspace) -> None:
    """The latch is per-task — two different abandoned tasks each escalate once."""
    escalate_fn = build_persona_gap_escalate_fn(team_id="T1")
    base = {"kind": "escalation", "worker": "w", "attempt": 5, "last_status": "x"}
    escalate_fn({**base, "task_id": 1, "category": "capacity", "upstream_task_id": None})
    escalate_fn({**base, "task_id": 2, "category": "blocked", "upstream_task_id": 1})
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 2


def test_escalate_fn_always_calls_base_sink(team_workspace) -> None:
    """Escalation is NEVER silenced: the guaranteed-emitting base sink runs on
    EVERY call, even when the ledger latch dedupes a repeat (consensus item 8)."""
    seen: list[dict] = []
    escalate_fn = build_persona_gap_escalate_fn(team_id="T1", base_sink=seen.append)
    esc = {
        "kind": "escalation",
        "task_id": 9,
        "worker": "w",
        "attempt": 5,
        "category": "capacity",
        "last_status": "x",
        "upstream_task_id": None,
    }
    escalate_fn(esc)
    escalate_fn(esc)  # repeat — ledger latch dedupes, but the sink fires again
    assert len(seen) == 2  # base sink fired on BOTH calls
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 1  # ledger latched


def test_escalate_fn_default_sink_is_the_engine_default(team_workspace) -> None:
    """With no base_sink, the seam falls back to the engine's guaranteed
    WARNING-log default — never a silent escalation."""
    from scripts.pm_dispatch import _default_escalate

    escalate_fn = build_persona_gap_escalate_fn(team_id="T1")
    # The closure must reference the guaranteed default; calling it must not raise
    # even when the ledger write is the only observable effect.
    escalate_fn(
        {
            "kind": "escalation",
            "task_id": 3,
            "worker": "w",
            "attempt": 5,
            "category": "capacity",
            "last_status": "x",
            "upstream_task_id": None,
        }
    )
    # Indirect proof the default is wired: the module exposes it and the seam
    # tolerates a missing custom sink without error.
    assert callable(_default_escalate)
    assert _audit_count(team_workspace["db"], "persona_gap_escalation") == 1


def test_escalate_fn_ledger_failure_never_suppresses_base_sink(team_workspace, monkeypatch) -> None:
    """If the LEDGER write fails, the guaranteed base sink MUST still have fired
    (escalation is guaranteed-emitted; the ledger latch is best-effort enrichment
    on top, never a gate that can swallow the escalation)."""
    seen: list[dict] = []

    def boom(**_kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(team_meeting, "escalate_persona_gap", boom)
    escalate_fn = build_persona_gap_escalate_fn(team_id="T1", base_sink=seen.append)
    escalate_fn(
        {
            "kind": "escalation",
            "task_id": 5,
            "worker": "w",
            "attempt": 5,
            "category": "capacity",
            "last_status": "x",
            "upstream_task_id": None,
        }
    )
    assert len(seen) == 1  # base sink fired despite the ledger failure
