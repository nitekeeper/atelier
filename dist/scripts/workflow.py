# scripts/workflow.py
from scripts.db import get_connection
from scripts.projects import update_project


class WorkflowError(Exception):
    pass


VALID_TRANSITIONS: dict[str, list[str]] = {
    "design:in-progress":            ["design:approved"],
    "design:approved":               ["plan:in-progress"],
    "plan:in-progress":              ["plan:approved"],
    "plan:approved":                 ["tdd:red"],
    "tdd:red":                       ["tdd:green"],
    "tdd:green":                     ["tdd:refactor"],
    "tdd:refactor":                  ["code-review:draft"],
    "code-review:draft":             ["code-review:changes-requested", "code-review:merged"],
    "code-review:changes-requested": ["code-review:draft"],
    "code-review:merged":            ["security-review:in-progress"],
    "security-review:in-progress":   ["security-review:approved"],
    "security-review:approved":      ["qa-review:in-progress"],
    "qa-review:in-progress":         ["qa-review:approved"],
    "qa-review:approved":            [],
}

PHASE_GATES: dict[str, str | None] = {
    "dev:design":          None,
    "dev:plan":            "design:approved",
    "dev:tdd-red":         "plan:approved",
    "dev:tdd-green":       None,
    "dev:tdd-refactor":    "tdd:green",
    "dev:code-review":     "tdd:refactor",
    "dev:security-review": "code-review:merged",
    "dev:qa-review":       "security-review:approved",
    "dev:diagnose":        None,
    "dev:handoff":         None,
}


def get_phase(db_path: str, project_id: int) -> str:
    conn = get_connection(db_path)
    row = conn.execute("SELECT phase FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if row is None:
        raise WorkflowError(f"Project {project_id} not found")
    return row[0]


def advance_phase(db_path: str, project_id: int, new_phase: str) -> str:
    current = get_phase(db_path, project_id)
    allowed = VALID_TRANSITIONS.get(current, [])
    if new_phase not in allowed:
        raise WorkflowError(
            f"Invalid transition: '{current}' → '{new_phase}'. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )
    update_project(db_path, project_id, phase=new_phase)
    return new_phase


def check_gate(db_path: str, project_id: int, required_phase: str | None) -> None:
    if required_phase is None:
        return
    current = get_phase(db_path, project_id)
    if current != required_phase:
        raise WorkflowError(
            f"Gate not met: project is at '{current}', requires '{required_phase}'"
        )


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1]
    db_path = ".ai/memex.db"

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
        command = sys.argv[3]
        required = PHASE_GATES.get(command)
        try:
            check_gate(db_path, project_id, required)
            print(f"Gate passed. Current phase: {get_phase(db_path, project_id)}")
        except WorkflowError as e:
            print(f"Gate failed: {e}")
            sys.exit(1)

    elif cmd == "force-phase":
        project_id = int(sys.argv[2])
        new_phase = sys.argv[3]
        from scripts.projects import update_project
        update_project(db_path, project_id, phase=new_phase)
        print(f"Phase forced to: {new_phase}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
