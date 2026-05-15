# scripts/workflow.py
"""DB-backed phase state machine.

Phases and valid transitions are stored in the phases and phase_transitions
tables (seeded by migration 003). This replaces the hardcoded VALID_TRANSITIONS
and PHASE_GATES dicts.
"""
from __future__ import annotations

import json
import sys
from contextlib import closing
from dataclasses import dataclass
from scripts.db import get_connection
from scripts.projects import update_project


@dataclass(frozen=True)
class GateResult:
    """Result of a phase gate check.

    `allowed=True` means the skill may proceed immediately.
    `allowed=False` means a soft wall is hit; caller should ask user
    to confirm bypass and then call `log_bypass`.
    """
    allowed: bool
    current_phase: str
    required_phase: str | None
    reason: str

    def __str__(self) -> str:
        if self.allowed:
            return f"allowed at {self.current_phase}"
        return f"BLOCKED: {self.current_phase} requires {self.required_phase}"


class WorkflowError(Exception):
    pass


def get_phase(db_path: str, project_id: int) -> str:
    """Return the current phase of a project."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT phase FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError(f"Project {project_id} not found")
        return row[0]
    finally:
        conn.close()


def get_valid_transitions(db_path: str, from_phase: str) -> list[str]:
    """Return list of phases reachable from from_phase."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT to_phase FROM phase_transitions WHERE from_phase = ?",
            (from_phase,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def is_allow_from_any(db_path: str, phase: str) -> bool:
    """Return True if the phase can be entered from any current phase."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT allow_from_any FROM phases WHERE name = ?", (phase,)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def advance_phase(db_path: str, project_id: int, new_phase: str) -> str:
    """Advance project to new_phase, enforcing valid transitions.

    Phases with allow_from_any=True (e.g. diagnose:open) bypass the
    transition check and can be entered from any current phase.
    """
    current = get_phase(db_path, project_id)
    if not is_allow_from_any(db_path, new_phase):
        allowed = get_valid_transitions(db_path, current)
        if new_phase not in allowed:
            raise WorkflowError(
                f"Invalid transition: '{current}' → '{new_phase}'. "
                f"Allowed: {allowed or ['none (terminal state)']}"
            )
    update_project(db_path, project_id, phase=new_phase)
    return new_phase


def check_gate(db_path: str, project_id: int, skill: str) -> GateResult:
    """Check whether `skill` is in-phase for `project_id`.

    Returns a GateResult describing the outcome. Does NOT raise on a phase mismatch.
    Callers decide whether to proceed (typically: confirm with user, log bypass,
    then proceed).

    May raise WorkflowError if `project_id` does not exist (programming error,
    not a soft-wall concern — calling skills should validate the project exists
    before invoking check_gate).
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT required_phase FROM skill_gates WHERE skill = ?", (skill,)
        ).fetchone()
    finally:
        conn.close()

    current = get_phase(db_path, project_id)

    # No row, or row's required_phase is NULL -> no gate
    if row is None or row[0] is None:
        return GateResult(
            allowed=True,
            current_phase=current,
            required_phase=None,
            reason="No gate configured for this skill",
        )

    required = row[0]
    if current == required:
        return GateResult(
            allowed=True,
            current_phase=current,
            required_phase=required,
            reason=f"Project at '{current}' satisfies the gate",
        )

    return GateResult(
        allowed=False,
        current_phase=current,
        required_phase=required,
        reason=(
            f"Project is at '{current}', this skill normally requires '{required}'. "
            "Bypass is available — confirm with user before proceeding."
        ),
    )


def log_bypass(
    db_path: str,
    project_id: int,
    skill: str,
    current_phase: str,
    required_phase: str,
    agent_id: str | None = None,
    note: str | None = None,
) -> int:
    """Log a soft-wall bypass to phase_bypasses.

    Idempotent: if a row with the same (project, skill, current_phase,
    required_phase) was written within the last 60 seconds, returns that
    row's id instead of inserting a new one.

    Race condition note: the idempotency check uses a SELECT-then-INSERT
    pattern. Under concurrent access, two callers could both pass the SELECT
    before either INSERTs, resulting in duplicate rows. This race is
    intentionally tolerated — soft-wall bypass logging is a rare, low-stakes
    event and deduplication is best-effort. The window check prevents the
    common case of accidental double-invocation from the same caller.
    """
    with closing(get_connection(db_path)) as conn:
        existing = conn.execute(
            """SELECT id FROM phase_bypasses
               WHERE project_id = ? AND skill = ?
                 AND current_phase = ? AND required_phase = ?
                 AND bypassed_at >= datetime('now', '-60 seconds')
               LIMIT 1""",
            (project_id, skill, current_phase, required_phase),
        ).fetchone()
        if existing is not None:
            return existing[0]

        cursor = conn.execute(
            """INSERT INTO phase_bypasses
                 (project_id, skill, current_phase, required_phase, agent_id, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, skill, current_phase, required_phase, agent_id, note),
        )
        conn.commit()
        assert cursor.lastrowid is not None, "INSERT returned no rowid"
        return cursor.lastrowid


if __name__ == "__main__":
    # Usage: workflow.py <db_path> <command> [args...]
    # db_path defaults to .ai/memex.db when invoked without it.
    # All CLI tests pass it explicitly.
    if len(sys.argv) >= 3 and not sys.argv[1].startswith("-") and sys.argv[2] in (
        "get-phase", "advance", "check-gate", "force-phase", "transitions", "log-bypass"
    ):
        db_path = sys.argv[1]
        cmd = sys.argv[2]
        _argv_offset = 3
    else:
        db_path = ".ai/memex.db"
        cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
        _argv_offset = 2

    if cmd == "get-phase":
        project_id = int(sys.argv[_argv_offset])
        print(get_phase(db_path, project_id))

    elif cmd == "advance":
        project_id = int(sys.argv[_argv_offset])
        new_phase = sys.argv[_argv_offset + 1]
        try:
            result = advance_phase(db_path, project_id, new_phase)
            print(f"Phase advanced to: {result}")
        except WorkflowError as e:
            print(f"Error: {e}")
            sys.exit(1)

    # CLI exit code: ALWAYS 0 for check-gate, regardless of allowed=False.
    # This is a deliberate breaking change from the prior contract where
    # gate-not-met → exit 1. Consumers must parse the JSON `allowed` field.
    # Documented in CHANGELOG via Task 14.
    elif cmd == "check-gate":
        project_id = int(sys.argv[_argv_offset])
        skill = sys.argv[_argv_offset + 1]
        result = check_gate(db_path, project_id, skill)
        print(json.dumps({
            "allowed": result.allowed,
            "current_phase": result.current_phase,
            "required_phase": result.required_phase,
            "reason": result.reason,
        }, sort_keys=True))
        sys.exit(0)  # always 0 -- "not allowed" is no longer an error

    elif cmd == "force-phase":
        project_id = int(sys.argv[_argv_offset])
        new_phase = sys.argv[_argv_offset + 1]
        update_project(db_path, project_id, phase=new_phase)
        print(f"Phase forced to: {new_phase}")

    elif cmd == "transitions":
        from_phase = sys.argv[_argv_offset]
        transitions = get_valid_transitions(db_path, from_phase)
        print(f"From '{from_phase}': {transitions}")

    elif cmd == "log-bypass":
        project_id = int(sys.argv[_argv_offset])
        skill = sys.argv[_argv_offset + 1]
        current_phase = sys.argv[_argv_offset + 2]
        required_phase = sys.argv[_argv_offset + 3]
        agent_id = None
        note = None
        i = _argv_offset + 4
        while i < len(sys.argv):
            if sys.argv[i] in ("--agent", "--note"):
                if i + 1 >= len(sys.argv):
                    print(f"{sys.argv[i]} requires a value", file=sys.stderr)
                    sys.exit(1)
                flag_name = sys.argv[i]
                flag_value = sys.argv[i + 1]
                if flag_name == "--agent":
                    agent_id = flag_value
                else:
                    note = flag_value
                i += 2
            else:
                print(f"Unknown argument: {sys.argv[i]}", file=sys.stderr)
                sys.exit(1)
        bypass_id = log_bypass(
            db_path, project_id, skill, current_phase, required_phase,
            agent_id=agent_id, note=note,
        )
        print(json.dumps({"bypass_id": bypass_id}, sort_keys=True))
        sys.exit(0)

    else:
        print("Commands: get-phase, advance, check-gate, force-phase, transitions, log-bypass")
        sys.exit(1)
