# scripts/workflow.py
"""Phase-gate workflow — routes writes through the backend facade.

`transition_phase` and `record_phase_bypass` go through
`scripts.backend.*` (mode-dispatched between Memex and Local). Phase
catalog reads (the `phases`, `phase_transitions`, `skill_gates`,
`projects.phase` columns) still query the workspace DB directly:
the catalog is static, mode-symmetric, and read-only on the hot path.

Soft-wall semantics (CLAUDE.md hard rule): `check_gate` is ADVISORY —
it returns a `GateResult` and NEVER raises on phase mismatch. It may
raise `WorkflowError` for an unknown `project_id` (a programming
error, not a soft-wall concern). `advance_phase` validates the
transition graph and raises `WorkflowError` on an invalid transition.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from scripts import backend, mode_detector


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
    """Raised on invalid phase transitions or unknown project_id.

    NOT raised on phase-mismatch in `check_gate` — that returns a
    `GateResult(allowed=False)` per spec §3 (soft walls)."""


# ── Catalog reads (static, mode-symmetric) ─────────────────────────────────


def _catalog_query(sql: str, params: tuple = ()) -> list[dict]:
    """SELECT-only read against the workspace DB.

    Memex mode delegates to `backend_memex._memex_core_raw_query`
    (which routes through `memex_stores.query("atelier", ...)` UNDER the
    Memex call shim — deferred call-time `scripts.*` imports in Memex >=
    2.10 require it; see that helper's docstring); Local mode opens a
    connection against `<workspace-root>/.ai/atelier.db` via
    `backend_local._conn()`. Both targets share the same v1.1.0 schema,
    so the same SQL works against either."""
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        return backend_memex._memex_core_raw_query(store="atelier", sql=sql, params=params)
    from scripts import backend_local

    c = backend_local._conn()
    try:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()
    return rows


def get_phase(db_path: str, project_id: int) -> str:
    """Return the current phase of a project.

    `db_path` is retained for back-compat; the backend determines storage
    via mode detection. Raises `WorkflowError` if the project is missing."""
    rows = _catalog_query("SELECT phase FROM projects WHERE id = ?", (project_id,))
    if not rows:
        raise WorkflowError(f"Project {project_id} not found")
    return rows[0]["phase"]


def get_valid_transitions(db_path: str, from_phase: str) -> list[str]:
    """Return list of phases reachable from `from_phase`."""
    rows = _catalog_query(
        "SELECT to_phase FROM phase_transitions WHERE from_phase = ?", (from_phase,)
    )
    return [r["to_phase"] for r in rows]


def is_allow_from_any(db_path: str, phase: str) -> bool:
    """Return True if the phase can be entered from any current phase."""
    rows = _catalog_query("SELECT allow_from_any FROM phases WHERE name = ?", (phase,))
    return bool(rows and rows[0]["allow_from_any"])


# ── Writes (through the backend facade) ────────────────────────────────────


def advance_phase(
    db_path: str, project_id: int, new_phase: str, agent_id: str = "atelier-system"
) -> str:
    """Advance `project_id` to `new_phase`, enforcing valid transitions.

    Phases with `allow_from_any=True` (e.g. `diagnose:open`) bypass the
    transition graph and can be entered from any current phase. The DB
    write goes through `backend.transition_phase` so Memex- and Local-
    mode both follow the same write path.

    Raises `WorkflowError` on an invalid transition. The transition
    graph check is intentionally done here (not inside the backend) so
    Local + Memex backends stay "dumb writes" — they trust the caller
    (spec §3 / `backend_local.transition_phase` docstring)."""
    current = get_phase(db_path, project_id)
    if not is_allow_from_any(db_path, new_phase):
        allowed = get_valid_transitions(db_path, current)
        if new_phase not in allowed:
            raise WorkflowError(
                f"Invalid transition: '{current}' → '{new_phase}'. "
                f"Allowed: {allowed or ['none (terminal state)']}"
            )
    backend.transition_phase(
        project_id=project_id,
        to_phase=new_phase,
        agent_id=agent_id,
    )
    return new_phase


def check_gate(db_path: str, project_id: int, skill: str) -> GateResult:
    """Check whether `skill` is in-phase for `project_id`.

    Returns a GateResult describing the outcome. Does NOT raise on a
    phase mismatch. Callers decide whether to proceed (typically:
    confirm with user, log bypass, then proceed).

    May raise WorkflowError if `project_id` does not exist (programming
    error, not a soft-wall concern — calling skills should validate the
    project exists before invoking check_gate)."""
    current = get_phase(db_path, project_id)

    gate_rows = _catalog_query("SELECT required_phase FROM skill_gates WHERE skill = ?", (skill,))

    # No row, or row's required_phase is NULL -> no gate
    if not gate_rows or gate_rows[0]["required_phase"] is None:
        return GateResult(
            allowed=True,
            current_phase=current,
            required_phase=None,
            reason="No gate configured for this skill",
        )

    required = gate_rows[0]["required_phase"]
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
            f"Project is at '{current}', this skill normally requires "
            f"'{required}'. Bypass is available — confirm with user "
            f"before proceeding."
        ),
    )


def log_bypass(
    db_path: str,
    project_id: int,
    from_phase: str,
    to_phase: str,
    reason: str,
    agent_id: str,
) -> int:
    """Log a soft-wall bypass through `backend.record_phase_bypass`.

    Signature matches the v1.1.0 schema (`from_phase`, `to_phase`,
    `reason`, `agent_id` — all NOT NULL). The pre-v1.1.0
    `(skill, current_phase, required_phase, note)` shape is gone; the
    bypass row records the attempted transition, not the gated skill.

    Returns the new row id. Each call is a distinct audit event — no
    idempotency window (the v1.0.13 60-second dedup window is dropped
    in v1.1.0 because the backend is the single source of truth and
    the column it keyed off (`bypassed_at`) no longer exists)."""
    row = backend.record_phase_bypass(
        project_id=project_id,
        from_phase=from_phase,
        to_phase=to_phase,
        reason=reason,
        agent_id=agent_id,
    )
    return int(row["id"])


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_kv_flags(argv: list[str], names: tuple[str, ...]) -> dict[str, str]:
    """Parse `--flag value` pairs from argv. Unknown flags exit 1."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in names:
            if i + 1 >= len(argv):
                print(f"{flag} requires a value", file=sys.stderr)
                sys.exit(1)
            out[flag.lstrip("-")] = argv[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {flag}", file=sys.stderr)
            sys.exit(1)
    return out


if __name__ == "__main__":
    # Usage: workflow.py [<db_path>] <command> [args...]
    # db_path defaults to .ai/atelier.db when omitted; it is accepted but
    # not consumed (the backend determines storage via mode detection).
    if (
        len(sys.argv) >= 3
        and not sys.argv[1].startswith("-")
        and sys.argv[2]
        in (
            "get-phase",
            "advance",
            "check-gate",
            "force-phase",
            "transitions",
            "log-bypass",
        )
    ):
        db_path = sys.argv[1]
        cmd = sys.argv[2]
        _argv_offset = 3
    else:
        db_path = ".ai/atelier.db"
        cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
        _argv_offset = 2

    if cmd == "get-phase":
        project_id = int(sys.argv[_argv_offset])
        print(get_phase(db_path, project_id))

    elif cmd == "advance":
        project_id = int(sys.argv[_argv_offset])
        new_phase = sys.argv[_argv_offset + 1]
        flags = _parse_kv_flags(sys.argv[_argv_offset + 2 :], ("--agent",))
        agent_id = flags.get("agent", "atelier-system")
        try:
            result = advance_phase(db_path, project_id, new_phase, agent_id)
            print(f"Phase advanced to: {result}")
        except WorkflowError as e:
            print(f"Error: {e}")
            sys.exit(1)

    # CLI exit code: ALWAYS 0 for check-gate, regardless of allowed=False.
    # Consumers must parse the JSON `allowed` field.
    elif cmd == "check-gate":
        project_id = int(sys.argv[_argv_offset])
        skill = sys.argv[_argv_offset + 1]
        result = check_gate(db_path, project_id, skill)
        print(
            json.dumps(
                {
                    "allowed": result.allowed,
                    "current_phase": result.current_phase,
                    "required_phase": result.required_phase,
                    "reason": result.reason,
                },
                sort_keys=True,
            )
        )
        sys.exit(0)

    elif cmd == "force-phase":
        project_id = int(sys.argv[_argv_offset])
        new_phase = sys.argv[_argv_offset + 1]
        flags = _parse_kv_flags(sys.argv[_argv_offset + 2 :], ("--agent",))
        agent_id = flags.get("agent", "atelier-system")
        # `force-phase` skips the transition graph but still routes
        # through the backend for the actual write — matches the rest
        # of the module's "all writes through the facade" rule.
        backend.transition_phase(
            project_id=project_id,
            to_phase=new_phase,
            agent_id=agent_id,
        )
        print(f"Phase forced to: {new_phase}")

    elif cmd == "transitions":
        from_phase = sys.argv[_argv_offset]
        transitions = get_valid_transitions(db_path, from_phase)
        print(f"From '{from_phase}': {transitions}")

    elif cmd == "log-bypass":
        project_id = int(sys.argv[_argv_offset])
        from_phase = sys.argv[_argv_offset + 1]
        to_phase = sys.argv[_argv_offset + 2]
        flags = _parse_kv_flags(
            sys.argv[_argv_offset + 3 :],
            ("--reason", "--agent"),
        )
        if "reason" not in flags or "agent" not in flags:
            print("log-bypass requires --reason and --agent", file=sys.stderr)
            sys.exit(1)
        bypass_id = log_bypass(
            db_path,
            project_id,
            from_phase,
            to_phase,
            reason=flags["reason"],
            agent_id=flags["agent"],
        )
        print(json.dumps({"bypass_id": bypass_id}, sort_keys=True))
        sys.exit(0)

    else:
        print("Commands: get-phase, advance, check-gate, force-phase, transitions, log-bypass")
        sys.exit(1)
