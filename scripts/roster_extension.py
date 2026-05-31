# scripts/roster_extension.py
"""Roster-extension consent flow (atelier#64 AI-4; design §11.3).

PM may propose a NEW persona mid-run when a gap surfaces that no roster role
fills. Per §11.3, registering a new persona is a PERMANENT change affecting
future runs, so it requires EXPLICIT human confirmation. The flow:

    PM emits  _mtype='propose_role'   (the proposal — rationale + name + desc)
        │
        ▼
    orchestrator surfaces it for the human, who acks
              _mtype='propose_role_ack'   (the consent decision)
        │
        ▼
    ON ACK ONLY → write the persona to the LOCAL roster via
                  backend.find_or_create_role(name=…, description=…)
                  (roles.name is UNIQUE → idempotent)

## The consent gate is a RECORDED human-ack event, not a marker parse (AC5)

``write_proposed_role`` writes the persona ONLY when a recorded
``event_type='roster_consent'`` row with ``acked=True`` exists in
``team_audit_log`` for this (team, role name). It does NOT gate on the mere
presence / parse of a ``propose_role`` marker — an injected proposal cannot
bypass the gate because the gate counts a LEDGER consent row, and that row is
written by ``record_ack`` (the orchestrator's human-ack handler), never by
proposal content.

* propose → NO ack → role is NOT written (the consent guarantee, NN1).
* propose → ack → role written + available, and the consent decision (rationale
  + ack + role) is recorded in ``team_audit_log`` (AC1).
* propose → ack → ack again → idempotent: ``roles.name`` UNIQUE means the second
  write returns the existing row; no duplicate role, no duplicate persona.

This roster write (``roles`` table in the always-Local ``.ai/atelier.db`` /
agents.db) is DISTINCT from ``persona_snapshots`` (minted later, at spawn time).
"""

from __future__ import annotations

from scripts import backend

PROPOSE_EVENT_TYPE = "roster_proposal"
CONSENT_EVENT_TYPE = "roster_consent"


def record_proposal(
    *,
    team_id: str,
    role_name: str,
    role_description: str,
    rationale: str,
) -> dict:
    """Record a PM ``propose_role`` proposal in ``team_audit_log``.

    This is CAPTURE only — it never writes a role. It exists so the consent
    decision later references a recorded proposal (audit completeness). The
    persona write gates on the CONSENT row, not this proposal row.
    """
    return backend.write_team_audit(
        team_id=team_id,
        event_type=PROPOSE_EVENT_TYPE,
        payload={
            "role_name": role_name,
            "role_description": role_description,
            "rationale": rationale,
        },
    )


def record_ack(
    *,
    team_id: str,
    role_name: str,
    acked: bool,
    rationale: str,
) -> dict:
    """Record the human consent decision (ack OR no-ack) in ``team_audit_log``.

    This is the ONLY place a ``roster_consent`` row is written, and it is the
    sole gate ``write_proposed_role`` honors. ``acked`` carries the human's
    yes/no; ``rationale`` is the recorded justification (§11.3). A no-ack is
    recorded too, so the audit trail shows the decision was made and refused —
    not merely that nothing happened.
    """
    return backend.write_team_audit(
        team_id=team_id,
        event_type=CONSENT_EVENT_TYPE,
        payload={"role_name": role_name, "acked": bool(acked), "rationale": rationale},
    )


def has_recorded_ack(*, team_id: str, role_name: str) -> bool:
    """True iff a recorded ``roster_consent`` row with ``acked=True`` exists for
    this (team, role name). The consent gate — counts LEDGER rows, never a
    marker parse, so an injected proposal cannot fabricate consent (AC5)."""
    for row in backend.list_team_audit(team_id=team_id, event_type=CONSENT_EVENT_TYPE):
        payload = _row_payload(row)
        if payload.get("role_name") == role_name and payload.get("acked") is True:
            return True
    return False


def write_proposed_role(
    *,
    team_id: str,
    role_name: str,
    role_description: str,
) -> dict | None:
    """Write the proposed persona to the LOCAL roster — ONLY if a recorded
    human ack exists (the consent gate).

    Returns the role row when written/already-present, or ``None`` when no
    recorded ack gates the write (the consent guarantee — NN1). Idempotent on
    re-invocation: ``backend.find_or_create_role`` keys on the UNIQUE
    ``roles.name`` so a second ack-then-write returns the existing row, never a
    duplicate (AC1 idempotency).
    """
    if not has_recorded_ack(team_id=team_id, role_name=role_name):
        # propose → NO ack → NOT written. An injected proposal that never
        # earned a recorded consent row cannot reach the roster.
        return None
    return backend.find_or_create_role(name=role_name, description=role_description)


def _row_payload(row: dict) -> dict:
    """Decode a team_audit_log row's JSON payload to a dict (empty on None /
    malformed). The ledger stores payload as a JSON TEXT column."""
    import json

    raw = row.get("payload")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
