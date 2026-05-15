# scripts/workflow.py
"""DB-backed phase state machine.

Phases and valid transitions are stored in the phases and phase_transitions
tables (seeded by migration 003). This replaces the hardcoded VALID_TRANSITIONS
and PHASE_GATES dicts.
"""
from __future__ import annotations

import json
import sys
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

    Returns a GateResult describing the outcome. Does NOT raise on mismatch.
    Callers decide whether to proceed (typically: confirm with user, log bypass,
    then proceed).
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


if __name__ == "__main__":
    db_path = ".ai/atelier.db"
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "get-phase":
        project_id = int(sys.argv[2])
        print(get_phase(db_path, project_id))

    elif cmd == "advance":
        project_id = int(sys.argv[2])
        new_phase = sys.argv[3]
        try:
            result = advance_phase(db_path, project_id, new_phase)
            print(f"Phase advanced to: {result}")
        except WorkflowError as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif cmd == "check-gate":
        project_id = int(sys.argv[2])
        skill = sys.argv[3]
        result = check_gate(db_path, project_id, skill)
        print(json.dumps({
            "allowed": result.allowed,
            "current_phase": result.current_phase,
            "required_phase": result.required_phase,
            "reason": result.reason,
        }))
        sys.exit(0)  # always 0 -- "not allowed" is no longer an error

    elif cmd == "force-phase":
        project_id = int(sys.argv[2])
        new_phase = sys.argv[3]
        update_project(db_path, project_id, phase=new_phase)
        print(f"Phase forced to: {new_phase}")

    elif cmd == "transitions":
        from_phase = sys.argv[2]
        transitions = get_valid_transitions(db_path, from_phase)
        print(f"From '{from_phase}': {transitions}")

    else:
        print("Commands: get-phase, advance, check-gate, force-phase, transitions")
        sys.exit(1)
