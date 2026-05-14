# scripts/workflow.py
"""DB-backed phase state machine.

Phases and valid transitions are stored in the phases and phase_transitions
tables (seeded by migration 003). This replaces the hardcoded VALID_TRANSITIONS
and PHASE_GATES dicts.
"""
from __future__ import annotations

import sys
from scripts.db import get_connection
from scripts.projects import update_project


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


def check_gate(db_path: str, project_id: int, skill: str) -> None:
    """Raise WorkflowError if the project's current phase does not satisfy
    the skill's entry gate. Passes silently if no gate is configured (NULL).
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT required_phase FROM skill_gates WHERE skill = ?", (skill,)
        ).fetchone()
    finally:
        conn.close()

    if row is None or row[0] is None:
        return  # No gate configured — always passes

    required_phase = row[0]
    current = get_phase(db_path, project_id)
    if current != required_phase:
        raise WorkflowError(
            f"Gate not met: project is at '{current}', requires '{required_phase}'"
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
        try:
            check_gate(db_path, project_id, skill)
            print(f"Gate passed. Current phase: {get_phase(db_path, project_id)}")
        except WorkflowError as e:
            print(f"Gate failed: {e}")
            sys.exit(1)

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
