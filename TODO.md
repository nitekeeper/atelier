# TODO

Deferred work tracked here so it survives session boundaries. Cross out items as they land or get explicitly dropped.

## Deterministic-host CLI sandbox prerequisites (per platform)

The deterministic-host engine confines autonomous `claude -p` agents with **Claude Code's native sandbox** (`cli_dispatch.native_sandbox_wrap`, fail-closed). Prerequisites are **per-platform** (verified against code.claude.com/docs/en/sandboxing); availability is detected by `sandbox_runtime_available()` / `sandbox_prereq_status()`:

- **Linux / WSL2** → install **bubblewrap + socat**: `sudo apt install bubblewrap socat` (bwrap = filesystem isolation; socat = the network-proxy relay for `network.allowedDomains`). On Ubuntu 24.04+ also add the AppArmor `bwrap` userns profile (see the doc's accordion).
- **macOS** → **nothing to install**; the built-in Seatbelt framework is used.
- **Native Windows** → unsupported; run atelier under **WSL2** (reports as `linux`, uses the bwrap+socat path).

Without the runtime, the mandatory-sandbox gate (`UnsandboxedRealRunError`) fail-closes and the live e2e harness (`tests/test_e2e_live.py`, `-m live`) skips with a platform-correct reason.

## Cost/quality mode selection at atelier start (user request 2026-06-13)

Let the user pick a **cost-effective** vs **code-quality** configuration **when they start atelier** (e.g. at the top of `atelier:run` / when a run is kicked off), and have atelier **honor that choice for the run**. Rationale: some users have a limited budget, others don't — we want to satisfy both groups by letting them select the posture per run.

- [ ] **Add an at-start mode prompt to `atelier:run`** offering `cost-effective` | `code-quality`, with the selection stored in run/project state and read by the dispatch + model-selection path. Distinct from today's `settings-recommendation` offer, which fires **once per plugin version** on upgrade and writes global `~/.claude/settings.json` — this new one is a **per-run** choice.
- [ ] **Wire the chosen mode through the model + budget posture** (not just the orchestrator model):
  - orchestrator/host model: `cost-effective → sonnet`, `code-quality → opus` (mirrors the existing `recommended_settings.PROFILES`).
  - per-task `model_tier` posture: cost → lean toward haiku/sonnet; quality → opus-heavier (the mode parameterizes the `PHASE_TIER`/`ROLE_FLOOR`/`DIFFICULTY_TIER` tables).
  - in the deterministic-host engine (restructure in progress): the mode sets the `BudgetPool` ceiling/headroom + `static_fleet_width` posture (cost → tighter token budget + narrower fan-out; quality → looser/no cap).
- [ ] **Unify with the existing `settings-recommendation` profiles — do NOT create a conflicting second mechanism.** The saved global profile (`cost-effective`|`code-quality` in `scripts/recommended_settings.PROFILES`) becomes the **default**; the at-start prompt offers "use your saved profile (X)" or switch **for this run only**.
- [ ] **Avoid prompt fatigue:** remember the last choice and offer it as the Enter-default; provide a non-interactive override (env var or setting) so CI / scripted runs don't block; skippable.
- [ ] **Folds into the deterministic-host restructure at ~M6** (`model_tier` + `BudgetPool` wiring) — see `docs/plans/2026-06-13-atelier-v2-migration.md` (R-MODE). Can also ship standalone against the current engine; the restructure just makes the budget half real.

## Wave 0 Task 5 follow-ups (migrations split audit)

Findings from the reviewer + QA audit on commit `47f5b27` (now rebased / amended) that intentionally don't ship with the split itself.

- [x] **Reviewer Imp-3 — `tasks.priority` TEXT → INTEGER mismatch.** Resolved: `_coerce_priority` in `scripts/tasks.py` coerces all inputs (TEXT string, int, None) at every write seam; `test_tasks.py` covers all named-string, int, None, and unknown-string paths.
- [ ] **Reviewer Nit-1 — `idx_workspaces_identity` is redundant.** `workspaces.identity` is `UNIQUE NOT NULL`, so SQLite auto-indexes it; the explicit `CREATE INDEX idx_workspaces_identity` (shared schema line 28) duplicates that. Defer to a spec amendment — removing it now risks breaking consumers that drop the index by name. Add a one-line note when the spec is touched. — Deferred pending spec amendment; removing risks breaking consumers that drop the index by name.
- [ ] **Reviewer Nit-3 — `phase_bypasses.agent_id` no FK.** v1.0.13's `005_soft_walls.sql` declared `agent_id TEXT NOT NULL REFERENCES agents(id)`. v1.1.0 widens to `TEXT NOT NULL` so Memex-mode bypasses (where agents live in `~/.memex/agents.db`, not the workspace DB) can still log. The trade-off: audit-trail rows can now reference an agent that doesn't exist anywhere on disk. Reintroduce the FK if/when both modes share an agents source. — Deliberate trade-off: widened from FK to TEXT NOT NULL so Memex-mode bypass rows (agents in ~/.memex/agents.db) can log without a local FK.
- [ ] **Reviewer Nit-4 — `meeting_minutes.filename` is now nullable.** v1.0.13 required it; v1.1.0 makes it optional so DB-only minutes (no `.ai/meetings/*.md` export) are representable. Plan 4's legacy reader must default `NULL → ''` only if downstream code chokes on `None` — leave as `NULL` if callers handle it. — Deliberate: nullable allows DB-only minutes (no .md export). Callers use `meeting.get("filename") or ""` guard.

## Match the memex / agora gatekeeper setup

Memex and agora got the full CI + release-workflow treatment on 2026-05-16/17. Atelier didn't — when it's worth the time, replicate that pattern here so atelier is protected to the same standard. References below point at the memex commits to copy from.

- [x] **Add `pyproject.toml` with `[tool.ruff]` + `[tool.bandit]` config.** Copy the section structure from [memex's `pyproject.toml`](https://github.com/nitekeeper/memex/blob/main/pyproject.toml).
- [x] **Add `.github/workflows/ci.yml`** with three parallel jobs (lint, security, tests). Pin `actions/checkout` and `actions/setup-python` to SHAs from day one (`actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2`, `actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0`). Pattern lives at [memex's `ci.yml`](https://github.com/nitekeeper/memex/blob/main/.github/workflows/ci.yml).
- [x] **Add `.github/dependabot.yml`** for weekly pip + github-actions updates. Copy from memex.
- [x] **Run the baseline cleanup once:** `ruff check --fix .` then `ruff format .`, commit as a separate "style:" commit so the gates start from a clean baseline.
- [x] **Triage remaining Ruff + Bandit findings.** Either fix or `# nosec`/`# noqa` with justification — never blanket-suppress without a reason in the comment.

## When atelier ships its first release

- [x] **Add `scripts/bump.py` + `.github/workflows/release.yml`** mirroring memex's pattern. Bump script touches `.claude-plugin/plugin.json` + `pyproject.toml` + the dist manifest; release workflow fires on `v<X.Y.Z>` tag push and creates the GitHub Release + dispatches to agora.
- [ ] **Create `AGORA_DISPATCH_TOKEN` secret in this repo.** Same fine-grained PAT used for memex (scoped to `nitekeeper/agora`, Contents Read + Write, Metadata Read; 1-year expiry). Re-paste under *Settings → Secrets and variables → Actions* with the same exact name. — [CANNOT VERIFY — requires GitHub secrets access]
- [x] **Add the agora dispatch step to `release.yml`** so atelier releases auto-bump the agora pin. Same step memex's `release.yml` has — see [memex#10](https://github.com/nitekeeper/memex/pull/10) for the env-var-passing pattern that avoids the shell-injection class of bugs.
- [ ] **Add atelier to `PLUGIN_REPOS_READ_TOKEN`'s scope** if it's not already — that PAT lives in agora and currently covers `nitekeeper/memex` and `nitekeeper/atelier`. Verify atelier is in the list once atelier becomes a real release-shipping repo. — [CANNOT VERIFY — requires GitHub secrets access]

## When this repo goes public

Same post-public unlocks as memex / agora:

- [ ] Enable branch protection on `main` (`gh api -X PUT repos/nitekeeper/atelier/branches/main/protection ...`).
- [ ] Enable `allow_auto_merge` on the repo (`gh api -X PATCH repos/nitekeeper/atelier -F allow_auto_merge=true`).
- [ ] Enable CodeQL.
- [ ] (Optional) SonarCloud.
