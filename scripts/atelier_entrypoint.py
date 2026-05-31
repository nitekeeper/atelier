"""Shared startup check for Atelier user-facing skills — Plan 4 §Task 2.

Each of `skills/{load,save,ingest,run}/SKILL.md` calls this at the top
of its recipe. It returns an action token telling the skill what to do
BEFORE its actual work:

  * `proceed-local`    — Memex absent; carry on with the local backend.
                         All writes land in `<workspace>/.ai/atelier.db`.
  * `proceed-memex`    — Memex present + bootstrapped + no local DB to
                         migrate; carry on with the Memex backend.
                         Bootstrap is run lazily here so a fresh install
                         is provisioned the first time `startup_check`
                         lands on this branch.
  * `prompt-migration` — Memex present + project-local `.ai/atelier.db`
                         exists + neither the `.migrated` nor
                         `.local-only` marker is present. The skill must
                         surface the migration prompt via
                         `internal/migrate-local-to-memex/SKILL.md`
                         before continuing. After the user answers,
                         restart the pre-flight — `startup_check` will
                         return `proceed-memex` or `proceed-local`
                         depending on their choice.

The bootstrap import is intentionally lazy (inside the `proceed-memex`
branch). Importing it at module load would (a) defeat the test stub
on `scripts.bootstrap.run_bootstrap` and (b) drag the half-installed
Memex check into the `prompt-migration` path, which doesn't need it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scripts import mode_detector
from scripts.migrate_to_memex import row_summary, should_prompt


def _project_ai_dir() -> Path | None:
    """Walk upward from `Path.cwd()` looking for a `.git/` directory; on
    first hit, return `<that_dir>/.ai`. Returns None if no git root is
    reachable from CWD.

    Mirrors the resolver pattern in `scripts.backend_local._workspace_root`
    but stays local to this module: factoring a shared helper would force
    `backend_local` to import here (or vice-versa) and produce a
    needlessly wide diff. The walk is cheap, the duplication is two
    lines, and the divergence in failure semantics is intentional —
    `backend_local` raises `FileNotFoundError` because its callers need
    a DB; we return None because `startup_check` should silently
    degrade to "no migration prompt possible" rather than crash a
    skill's pre-flight.
    """
    cur = Path.cwd().resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur / ".ai"
        cur = cur.parent
    return None


def startup_check() -> dict:
    """Run the pre-flight check for an entry skill and return an action
    token. See the module docstring for the action contract.

    Resolution order matters:
      1. Local mode wins immediately — there's nothing to bootstrap or
         migrate when Memex isn't installed.
      2. In Memex mode, check for a migration-eligible local DB BEFORE
         calling `bootstrap.run_bootstrap`. A user with a stale local
         DB needs the prompt; bootstrapping Memex first would silently
         create a parallel Memex store that the user hasn't agreed to
         start using yet.
      3. Only on the clean Memex branch do we lazily import + run
         bootstrap.
    """
    mode = mode_detector.detect_mode()
    if mode == "local":
        return {"action": "proceed-local"}

    ai = _project_ai_dir()
    if ai is not None and should_prompt(ai):
        local_db = ai / "atelier.db"
        return {
            "action": "prompt-migration",
            "local_db": str(local_db),
            "summary": row_summary(local_db),
        }

    # Memex mode, nothing to migrate — bootstrap (lazy import keeps
    # the test stub on `scripts.bootstrap.run_bootstrap` effective).
    from scripts import bootstrap

    bootstrap_state = bootstrap.run_bootstrap()
    return {"action": "proceed-memex", "bootstrap": bootstrap_state}


# ── Live WaveDispatcher wiring (atelier#81, AI-4) ───────────────────────────
#
# The production construction of `pm_dispatch.WaveDispatcher` — the binding #60
# deferred ("every instantiation today is in tests"). This is the single
# orchestrator-entry factory that wires the mode-agnostic wave engine to the
# REAL queue-bridge transport: it selects the dispatch mode via
# `resolve_dispatch_mode`, builds a `spawn_fn` over the production
# `QueueBridgeDispatchTools`, builds the terminal-envelope `poll_fn`, and passes
# `escalate_fn` straight through. The engine, barrier, budget, and wall-clock
# are unchanged — this only supplies the three seams (#60/#61 own the rest).


def build_wave_dispatcher(
    db_path: str,
    *,
    team_pk: str,
    team_id: str,
    briefing_for: Callable[[Mapping[str, Any], int], str],
    members: list[str] | None = None,
    team_name: str | None = None,
    teammate_name_for: Callable[[Mapping[str, Any]], str] | None = None,
    escalate_fn: Callable[[Mapping[str, Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
    root: str | Path = ".",
):
    """Construct a live, mode-selected ``WaveDispatcher`` bound to the
    production queue-bridge transport (atelier#81).

    Resolution + wiring:

    1. ``mode = resolve_dispatch_mode(env, root)`` — env override → persisted
       marker → ``subagent`` default (atelier#62 precedence; unchanged here).
    2. ``tools = QueueBridgeDispatchTools(team_pk, db_path=db_path)`` — the
       production :class:`~scripts.dispatch.DispatchTools` binding: enqueues
       harness calls onto ``bridge_requests`` for the orchestrator turn-loop to
       service (``internal/bridge-poll/SKILL.md``).
    3. ``spawn_fn = build_spawn_fn(mode, tools=tools, ...)`` — the #61 factory
       (TeamCreate-once + first-touch handled inside; mode-branching internal).
    4. ``poll_fn = build_poll_fn(db_path, team_id=team_id, role_id_for=...)`` —
       the terminal-reply-envelope read over ``bridge_messages`` (fail-closed
       validation; ``None`` HOLDS the barrier).
    5. ``WaveDispatcher(db_path, spawn_fn=, poll_fn=, escalate_fn=)``.

    ``escalate_fn`` is threaded through as the **single unconditional path** —
    when ``None`` the engine's own guaranteed-emitting default
    (``pm_dispatch._default_escalate``) is used. No conditional escalation
    branches are added (consensus item 8: escalation is guaranteed-emitted).

    ``teammate_name_for`` maps a task to the teammate role-id; it is reused as
    the poll-side ``role_id_for`` so a worker's reply inbox and its spawn
    identity stay in lock-step. ``team_id`` is the cycle's team identity (the
    inbox the workers reply into); in agent-team mode it is the id ``create_team``
    returns — the orchestrator threads the same value here.

    Lazy imports keep ``pm_dispatch`` (which imports ``scripts.tasks`` and a
    chain of backend modules) out of the bare ``startup_check`` import path.
    """
    from scripts.dispatch import (
        QueueBridgeDispatchTools,
        build_poll_fn,
        build_spawn_fn,
        resolve_dispatch_mode,
    )
    from scripts.pm_dispatch import WaveDispatcher

    # Only forward `env` when the caller supplied one; otherwise let
    # resolve_dispatch_mode use its os.environ default (passing None would be a
    # non-Mapping and break the lookup).
    if env is not None:
        mode = resolve_dispatch_mode(env=env, root=root)
    else:
        mode = resolve_dispatch_mode(root=root)

    tools = QueueBridgeDispatchTools(team_pk, db_path=db_path)

    name_for = teammate_name_for or (lambda task: str(task["id"]))

    spawn_fn = build_spawn_fn(
        mode,
        tools=tools,
        briefing_for=briefing_for,
        members=members,
        team_name=team_name,
        teammate_name_for=name_for,
    )
    poll_fn = build_poll_fn(db_path, team_id=team_id, role_id_for=name_for)

    # escalate_fn is the single unconditional path: pass it through when given,
    # else fall back to the engine's guaranteed-emitting default (no branch).
    if escalate_fn is not None:
        return WaveDispatcher(db_path, spawn_fn=spawn_fn, poll_fn=poll_fn, escalate_fn=escalate_fn)
    return WaveDispatcher(db_path, spawn_fn=spawn_fn, poll_fn=poll_fn)
