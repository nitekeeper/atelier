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


# ── Default per-task model-tier seam (atelier model-tier selection) ─────────
#
# The production `model_for` seam: it binds `scripts.model_tier.recommend` to the
# orchestrator/task context. Phase is a per-CYCLE value (the tasks table has no
# `phase` column), so it is closed over from the caller's `phase` arg; role is
# the task's `assigned_to`; difficulty is read from an OPTIONAL per-task
# `difficulty` field when the planner sets one. The `ATELIER_MODEL_TIER` env pin
# is honored via the resolved env mapping. REUSED by the deterministic host's
# `cli_dispatch._host_model_for` (the only consumer since the M7 bridge-queue
# removal deleted the WaveDispatcher factories that previously called it).


def _default_model_for(
    phase: str | None,
    env: Mapping[str, str] | None,
    posture: str | None = None,
) -> Callable[[Mapping[str, Any], int], str | None]:
    """Build the default ``model_for(task, attempt) -> str | None`` seam.

    Sources the per-task tier from ``scripts.model_tier.recommend`` using the
    cycle ``phase`` (closed over), the task's assigned role (``task["assigned_to"]``,
    the dispatch role-id), and an OPTIONAL per-task ``difficulty`` field. The
    operator's ``ATELIER_MODEL_TIER`` env pin is honored: when the factory got an
    explicit ``env`` mapping we forward it; otherwise we fall back to the live
    ``os.environ`` so a pin set in the shell still wins.

    ``posture`` (M6b-2 R-MODE) — an OPTIONAL one-rung cost↔quality bias threaded
    into ``recommend`` (cost-lean / neutral / opus-lean). It defaults to ``None``
    (== neutral == NO transform). The HOST path (``cli_dispatch._host_model_for``)
    passes the resolved RunMode's posture here so the run-wide posture fans out
    per task while reusing this ONE shared policy.

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
            posture=posture,
        )

    return model_for
