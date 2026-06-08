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

import os
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


def _settings_rec_offer() -> dict | None:
    """Compute the version-upgrade settings-recommendation offer, MODE-AGNOSTIC.

    Lazily imports `scripts.recommended_settings` and calls its read-only
    `maybe_offer()` ONCE. The whole thing degrades to `None` on ANY error so a
    version-recommendation hiccup can never crash a skill's pre-flight — the
    offer is an additive nicety, not a load-bearing token. Unlike `resume_offer`
    (Local-only), this offer is attached on BOTH the proceed-local and
    proceed-memex branches: a plugin version upgrade is mode-independent.
    """
    try:
        from scripts import recommended_settings

        return recommended_settings.maybe_offer()
    except Exception:
        return None


def _write_active_project_file() -> None:
    """Write ``.ai/active_project`` so Atelier's hook scope gate works in
    Memex mode.

    In Local mode the project-creation SKILL writes this file when a
    project is created or opened. In Memex mode no SKILL writes it —
    the active project is tracked in ``~/.atelier/state.json`` and in
    the Memex DB. The hook scope gate (``hooks/context_budget.py`` and
    ``hooks/session_open.py``) reads only the file, so it always bails
    out silently for Memex sessions.

    Called from the ``proceed-memex`` branch of ``startup_check``, this
    writes a fixed sentinel so PostToolUse / PreCompact hooks know an
    Atelier session is active. The sentinel value ``"memex"`` is
    sufficient — the hooks only check presence and non-emptiness.

    Silently no-ops on any error — a write failure must not abort a
    session's pre-flight.
    """
    try:
        ai = _project_ai_dir()
        if ai is None:
            return
        ai.mkdir(parents=True, exist_ok=True)
        (ai / "active_project").write_text("memex", encoding="utf-8")
    except Exception:
        return


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

    Additive offers (never mutate the action-token contract):
      * `resume_offer` — LOCAL-only aborted-arc resume (atelier#66).
      * `settings_rec_offer` — MODE-AGNOSTIC version-upgrade settings
        recommendation; attached on BOTH proceed-local and proceed-memex when
        `recommended_settings.maybe_offer()` is non-None. Read-only: no write
        happens here; the consent y/N + apply live in
        `internal/settings-recommendation/SKILL.md`. NOT attached on the
        prompt-migration short-circuit (it surfaces on the next pass once the
        migration is decided).
    """
    mode = mode_detector.detect_mode()
    if mode == "local":
        result: dict = {"action": "proceed-local"}
        # ── Resume detection (atelier#66 [S2], AC3) — LOCAL branch ONLY ──────
        #
        # Detect an aborted-but-incomplete team-mode arc and, when found, attach
        # a `resume_offer` field ALONGSIDE the action token (additive — the
        # proceed-local contract is unchanged). This is LOCAL-only by design
        # (§17): a Memex-mode run has no Local team-mode dispatch state to
        # resume, so find_resumable_arc is never consulted on the Memex path.
        # NEVER-SILENT: this only ATTACHES an offer DATA token — the human is
        # asked new/continue by skills/run/SKILL.md; the detector mutates
        # nothing. Lazy import keeps `resume` (and its migrate chain) off the
        # bare `startup_check` import path; `_project_ai_dir()` resolves the
        # always-Local `.ai/atelier.db`. A None ai dir (no git root) yields no
        # offer — there is no project DB to inspect.
        ai = _project_ai_dir()
        if ai is not None:
            from scripts import resume

            offer = resume.find_resumable_arc(str(ai / "atelier.db"))
            if offer is not None:
                result["resume_offer"] = offer
        # Settings-recommendation offer (atelier settings-rec — MODE-AGNOSTIC):
        # attach the version-upgrade offer alongside the proceed-local action.
        # Read-only — the consent + apply happen in the SKILL, not here.
        settings_offer = _settings_rec_offer()
        if settings_offer is not None:
            result["settings_rec_offer"] = settings_offer
        return result

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
    memex_result: dict = {"action": "proceed-memex", "bootstrap": bootstrap_state}
    # Settings-recommendation offer (atelier settings-rec — MODE-AGNOSTIC):
    # attach the SAME version-upgrade offer on the Memex branch too. A plugin
    # version bump is mode-independent, so (unlike resume_offer) this is NOT
    # Local-only. Read-only — the consent + apply happen in the SKILL.
    settings_offer = _settings_rec_offer()
    if settings_offer is not None:
        memex_result["settings_rec_offer"] = settings_offer
    # Ensure .ai/active_project exists for the hook scope gate. In Local mode
    # the project-creation SKILL writes this; in Memex mode nothing does —
    # so we write a sentinel here at session start (idempotent, no-op on error).
    _write_active_project_file()
    return memex_result


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
    model_for: Callable[[Mapping[str, Any], int], str | None] | None = None,
    summarize_fn: Callable[[list[Mapping[str, Any]]], str] | None = None,
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

    ``model_for(task, attempt) -> str | None`` is the OPTIONAL per-task
    model-tier seam (additive, back-compatible). It is threaded straight into
    :func:`scripts.dispatch.build_spawn_fn`; ``None`` (the default here) means no
    model is attached to any spawn — byte-identical to the pre-policy behavior.
    The orchestrator-facing factory :func:`build_wave_dispatcher_for_project`
    builds a ``scripts.model_tier``-backed default; this lower-level factory
    leaves it unset unless the caller injects one.

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
        model_for=model_for,
    )
    poll_fn = build_poll_fn(db_path, team_id=team_id, role_id_for=name_for)

    # escalate_fn is the single unconditional path: pass it through when given,
    # else fall back to the engine's guaranteed-emitting default (no branch).
    # summarize_fn (wave-summary context compression) + env are threaded as
    # additive, back-compatible kwargs: `None` summarize_fn → the engine's
    # deterministic `default_wave_digest`; env carries ATELIER_COMPRESS_THRESHOLD.
    wd_kwargs: dict[str, Any] = {
        "spawn_fn": spawn_fn,
        "poll_fn": poll_fn,
        "summarize_fn": summarize_fn,
    }
    if escalate_fn is not None:
        wd_kwargs["escalate_fn"] = escalate_fn
    # Fall back to os.environ (mirrors build_spawn_fn_for_project) so a shell-set
    # ATELIER_COMPRESS_THRESHOLD still wins in the production caller, which passes
    # env=None. WaveDispatcher's own env=None default stays default-only, so
    # direct-construction unit tests remain isolated from the ambient shell.
    wd_kwargs["env"] = env if env is not None else os.environ
    return WaveDispatcher(db_path, **wd_kwargs)


# ── Default per-task model-tier seam (atelier model-tier selection) ─────────
#
# The production `model_for` seam: it binds `scripts.model_tier.recommend` to the
# orchestrator/task context the wave engine has. Phase is a per-CYCLE value (the
# tasks table has no `phase` column), so it is closed over from the factory's
# `phase` arg; role is the task's `assigned_to`; difficulty is read from an
# OPTIONAL per-task `difficulty` field when the planner sets one. The
# `ATELIER_MODEL_TIER` env pin is honored via the resolved env mapping.


def _default_model_for(
    phase: str | None,
    env: Mapping[str, str] | None,
) -> Callable[[Mapping[str, Any], int], str | None]:
    """Build the default ``model_for(task, attempt) -> str | None`` seam.

    Sources the per-task tier from ``scripts.model_tier.recommend`` using the
    cycle ``phase`` (closed over), the task's assigned role (``task["assigned_to"]``,
    the dispatch role-id), and an OPTIONAL per-task ``difficulty`` field. The
    operator's ``ATELIER_MODEL_TIER`` env pin is honored: when the factory got an
    explicit ``env`` mapping we forward it; otherwise we fall back to the live
    ``os.environ`` so a pin set in the shell still wins.

    Defensive: a task without ``assigned_to`` / ``difficulty`` simply omits that
    signal; ``recommend`` always returns a valid tier (never raises).
    """
    from scripts.model_tier import recommend

    resolved_env: Mapping[str, str] = env if env is not None else os.environ

    def model_for(task: Mapping[str, Any], attempt: int) -> str | None:
        role_id = task.get("assigned_to")
        difficulty = task.get("difficulty")
        return recommend(
            phase=phase,
            role_id=role_id if isinstance(role_id, str) else None,
            difficulty=difficulty if isinstance(difficulty, str) else None,
            env=resolved_env,
        )

    return model_for


# ── Live orchestrator-entry call site (atelier#85) ──────────────────────────
#
# #81 (PR #84) shipped `build_wave_dispatcher` above as a tested-but-DORMANT
# factory: it was never invoked from a live `/atelier:run` orchestrator path.
# This is the missing call site — the function the orchestrator turn-loop
# (`internal/dev-dispatch/SKILL.md`, routed from `skills/run/SKILL.md`) calls to
# obtain the live dispatcher for the current cycle. It composes the SAME #81
# production seams (`QueueBridgeDispatchTools` + `build_spawn_fn` +
# `build_poll_fn`) but exposes the orchestrator-context knobs `build_wave_dispatcher`
# hard-wires (a distinct reply-inbox `role_id_for`, a `teams_root` override for
# first-touch detection, and a `sleep_fn` for the poll cadence) plus a safe
# default `briefing_for`. It does NOT modify the #81 internals — it reuses them.


def build_wave_dispatcher_for_project(
    *,
    db_path: str,
    team_pk: str,
    team_id: str,
    briefing_for: Callable[[Mapping[str, Any], int], str] | None = None,
    members: list[str] | None = None,
    team_name: str | None = None,
    teammate_name_for: Callable[[Mapping[str, Any]], str] | None = None,
    role_id_for: Callable[[Mapping[str, Any]], str] | None = None,
    escalate_fn: Callable[[Mapping[str, Any]], None] | None = None,
    teams_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    root: str | Path = ".",
    sleep_fn: Callable[[float], None] | None = None,
    phase: str | None = None,
    model_for: Callable[[Mapping[str, Any], int], str | None] | None = None,
    summarize_fn: Callable[[list[Mapping[str, Any]]], str] | None = None,
):
    """Build the live, mode-selected ``WaveDispatcher`` for one cycle from an
    orchestrator/project context (atelier#85 — the missing #81 call site).

    Resolution + wiring mirrors :func:`build_wave_dispatcher` (env override →
    persisted ``.ai/atelier.mode`` marker → ``subagent`` default; production
    queue-bridge seams) but exposes the knobs the orchestrator turn-loop needs:

    * ``briefing_for(task, attempt) -> str`` — the spawn-prompt source. Defaults
      to a deterministic per-task stub so the dispatcher is constructible from a
      minimal call site; a real ``/atelier:run`` passes a
      :func:`scripts.dispatch.compose_briefing` wrapper (keeping the composer the
      single source of prompt text — it stays mode-agnostic per #61).
    * ``teammate_name_for(task) -> str`` — task → spawn-identity role-id
      (agent-team mode). Defaults to ``str(task["id"])``.
    * ``role_id_for(task) -> str`` — task → the inbox role-id whose
      ``bridge_messages`` channel carries the worker's terminal reply. Defaults to
      ``teammate_name_for`` so spawn-identity and reply-inbox stay in lock-step
      (the #81 default); the orchestrator overrides it when all replies land in a
      single PM inbox.
    * ``escalate_fn`` — threaded straight through (``None`` → the engine's
      guaranteed-emitting default; #87 binds the persona-gap escalation seam here
      via :func:`scripts.team_meeting.build_persona_gap_escalate_fn`).
    * ``teams_root`` / ``sleep_fn`` — first-touch config root + poll cadence
      (both injectable for deterministic tests).
    * ``phase`` — the cycle's CURRENT dev-arc phase (e.g. ``"review"``,
      ``"doc"``, ``"tdd:green"``). It is the per-cycle model-tier signal: the
      orchestrator turn-loop passes the wave's phase here the SAME way it sources
      the briefing context (the cycle phase is a wave-level value, not a per-task
      column). Used only by the default ``model_for`` seam.
    * ``model_for(task, attempt) -> str | None`` — the per-task model-tier seam
      (atelier model-tier selection). **Defaults** to a
      :func:`scripts.model_tier.recommend`-backed function that selects a tier
      alias (``haiku`` | ``sonnet`` | ``opus``) from the cycle ``phase`` + the
      task's assigned role (``task["assigned_to"]``) + an optional per-task
      ``difficulty`` field (honoring the ``ATELIER_MODEL_TIER`` env pin). Callers
      / tests may inject their own; pass an explicit lambda returning ``None`` to
      force the pre-policy (session-default) behavior. The chosen tier flows into
      the enqueued ``args_json`` and the bridge-poll servicer passes it to
      ``Agent(model=...)``.

    Lazy imports keep ``scripts.dispatch`` / ``pm_dispatch`` (and their backend
    chains) out of the bare ``startup_check`` import path.
    """
    from scripts.dispatch import (
        DEFAULT_TEAMS_ROOT,
        QueueBridgeDispatchTools,
        build_poll_fn,
        build_spawn_fn,
        resolve_dispatch_mode,
    )
    from scripts.pm_dispatch import WaveDispatcher

    if env is not None:
        mode = resolve_dispatch_mode(env=env, root=root)
    else:
        mode = resolve_dispatch_mode(root=root)

    name_for = teammate_name_for or (lambda task: str(task["id"]))
    # role_id_for falls back to the spawn-identity mapper (the #81 default):
    # spawn identity and reply inbox stay in lock-step unless the orchestrator
    # routes all replies into a single PM inbox.
    reply_for = role_id_for or name_for
    brief_for = briefing_for or (lambda task, attempt: f"task {task['id']} (attempt {attempt})")
    resolved_teams_root = teams_root if teams_root is not None else DEFAULT_TEAMS_ROOT

    # Per-task model-tier seam (atelier model-tier selection). Default it to a
    # scripts.model_tier.recommend-backed function; a caller may inject its own
    # (or a `lambda task, attempt: None` to force session-default spawns). The
    # default reads the cycle `phase`, the task's assigned role, and an optional
    # per-task `difficulty` field, honoring the ATELIER_MODEL_TIER env pin.
    pick_model = model_for if model_for is not None else _default_model_for(phase, env)

    tools = QueueBridgeDispatchTools(team_pk, db_path=db_path)
    spawn_fn = build_spawn_fn(
        mode,
        tools=tools,
        briefing_for=brief_for,
        members=members,
        team_name=team_name,
        teammate_name_for=name_for,
        teams_root=resolved_teams_root,
        model_for=pick_model,
    )
    poll_fn = build_poll_fn(db_path, team_id=team_id, role_id_for=reply_for)

    kwargs: dict[str, Any] = {"spawn_fn": spawn_fn, "poll_fn": poll_fn}
    if escalate_fn is not None:
        kwargs["escalate_fn"] = escalate_fn
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    # Wave-summary context compression seam (additive, back-compatible). `None`
    # → the engine's deterministic `default_wave_digest` (no LLM dependency); a
    # caller may inject a real summarizer later through this same seam. `env` is
    # threaded so the ATELIER_COMPRESS_THRESHOLD override is honored.
    kwargs["summarize_fn"] = summarize_fn
    # Fall back to os.environ so a shell-set ATELIER_COMPRESS_THRESHOLD wins in the
    # production caller (which passes env=None) — mirrors build_spawn_fn_for_project.
    kwargs["env"] = env if env is not None else os.environ
    return WaveDispatcher(db_path, **kwargs)
