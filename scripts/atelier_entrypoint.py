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

from pathlib import Path

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
