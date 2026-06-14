# scripts/roster.py
"""In-memory roster: persona → profile-text resolution for the CLI/host path.

The BRIDGE path resolves a worker's persona-profile body from the seeded
``agents.db`` (``seed_roles.py`` writes the 61-role roster; the bridge dispatch
reads ``agent_profile`` back out for ``compose_briefing(persona_profile_text=...)``).

The CLI/host path (``ATELIER_TRANSPORT=cli``) is DB-free at dispatch time — each
attempt is a one-shot ``claude -p`` subprocess and there is no live bridge/queue
servicer to do a DB lookup. M6a therefore needs an IN-MEMORY persona resolver so
``compose_briefing`` is fed the same profile body WITHOUT a DB read. This module
is that resolver.

## The key the roster is keyed on (the REAL planner field)

The planner emits, per task, an ``assigned_persona`` field whose VALUE is the
roster ``agent_id`` — e.g. ``"backend-engineer-1"``, ``"software-architect-1"``,
``"security-engineer-1"``. (Confirmed in ``scripts/planner.py``:
``persist_tasks`` calls ``tasks.create_task(assigned_to=t.get("assigned_persona"))``,
so the planner's ``assigned_persona`` IS what lands in the tasks ``assigned_to``
column, and every planner test fixture sets it to the full ``agent_id`` form.
``scripts/seed_roles.py``'s ``ROLES`` list keys each role on the SAME
``agent_id``.) The dict below is therefore keyed on ``agent_id``.

The mapping is explicit: ``assigned_persona`` (== ``agent_id``) → the role's
``agent_profile`` body. We build it FROM the single source of truth — the
``seed_roles.ROLES`` list — so the in-memory roster and the DB seed can never
drift (a new role added to ``ROLES`` is automatically in both).

## What this is NOT

It does NOT replace DB seeding for the bridge path (that stays exactly as-is —
``seed_roles.py`` is untouched). It is the additive, DB-free resolver the new CLI
factory wires into its briefing path.
"""

from __future__ import annotations

from scripts.seed_roles import ROLES

#: ``assigned_persona`` (== roster ``agent_id``, the REAL planner field value) →
#: that role's ``agent_profile`` body text. Built from the single source of truth
#: (``seed_roles.ROLES``) so it can never drift from the DB seed: every role the
#: bridge path seeds to ``agents.db`` is resolvable here with the SAME profile.
ROSTER_BY_PERSONA: dict[str, str] = {role["agent_id"]: role["agent_profile"] for role in ROLES}


class PersonaNotInRosterError(KeyError):
    """Raised by :func:`resolve_persona` when ``assigned_persona`` is unknown.

    A planner emitting a persona not in the 61-role roster is a DEFECT (the
    planner is constrained to the seeded roster), so this is FAIL-LOUD rather
    than silently substituting a placeholder — a wrong-persona briefing would
    quietly mis-identify the worker. The message names the unknown persona so the
    operator can correct the plan.
    """

    def __init__(self, assigned_persona: object) -> None:
        self._persona = assigned_persona
        super().__init__(
            f"assigned_persona {assigned_persona!r} is not in the in-memory roster "
            f"(known: {sorted(ROSTER_BY_PERSONA)!r})"
        )


def resolve_persona(assigned_persona: str | None) -> str:
    """Resolve a planner-emitted ``assigned_persona`` to its profile body text.

    ``assigned_persona`` is the planner field value — the roster ``agent_id``
    (e.g. ``"backend-engineer-1"``). Returns the role's ``agent_profile`` body
    (the SAME text the bridge path reads from ``agents.db``), with NO DB access.

    Raises :class:`PersonaNotInRosterError` for an unknown / ``None`` / non-string
    persona — fail-loud, because a wrong-persona briefing silently mis-identifies
    the worker (see the class docstring). The CLI factory's ``briefing_for``
    surfaces this as a per-task failed attempt (it runs inside ``pipeline``'s
    ``_run_one`` try-block), never a hung admission loop.
    """
    if not isinstance(assigned_persona, str):
        raise PersonaNotInRosterError(assigned_persona)
    try:
        return ROSTER_BY_PERSONA[assigned_persona]
    except KeyError as exc:
        raise PersonaNotInRosterError(assigned_persona) from exc
