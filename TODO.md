# TODO

Deferred work tracked here so it survives session boundaries. Cross out items as they land or get explicitly dropped.

## Wave 0 Task 5 follow-ups (migrations split audit)

Findings from the reviewer + QA audit on commit `47f5b27` (now rebased / amended) that intentionally don't ship with the split itself.

- [ ] **Reviewer Imp-3 — `tasks.priority` TEXT → INTEGER mismatch.** v1.0.13 stored priorities as strings (`'critical'|'high'|'medium'|'low'`); v1.1.0's schema (`migrations/shared/001_v110_schema.sql:86`) declares `priority INTEGER DEFAULT 0`. The footgun is now flagged with an inline comment, but `scripts/tasks.py` still inserts string literals. Plan 2 / Plan 3 will rewire callers and the legacy reader to convert.
- [ ] **Reviewer Nit-1 — `idx_workspaces_identity` is redundant.** `workspaces.identity` is `UNIQUE NOT NULL`, so SQLite auto-indexes it; the explicit `CREATE INDEX idx_workspaces_identity` (shared schema line 28) duplicates that. Defer to a spec amendment — removing it now risks breaking consumers that drop the index by name. Add a one-line note when the spec is touched.
- [ ] **Reviewer Nit-3 — `phase_bypasses.agent_id` no FK.** v1.0.13's `005_soft_walls.sql` declared `agent_id TEXT NOT NULL REFERENCES agents(id)`. v1.1.0 widens to `TEXT NOT NULL` so Memex-mode bypasses (where agents live in `~/.memex/agents.db`, not the workspace DB) can still log. The trade-off: audit-trail rows can now reference an agent that doesn't exist anywhere on disk. Reintroduce the FK if/when both modes share an agents source.
- [ ] **Reviewer Nit-4 — `meeting_minutes.filename` is now nullable.** v1.0.13 required it; v1.1.0 makes it optional so DB-only minutes (no `.ai/meetings/*.md` export) are representable. Plan 4's legacy reader must default `NULL → ''` only if downstream code chokes on `None` — leave as `NULL` if callers handle it.

## Match the memex / agora gatekeeper setup

Memex and agora got the full CI + release-workflow treatment on 2026-05-16/17. Atelier didn't — when it's worth the time, replicate that pattern here so atelier is protected to the same standard. References below point at the memex commits to copy from.

- [ ] **Add `pyproject.toml` with `[tool.ruff]` + `[tool.bandit]` config.** Copy the section structure from [memex's `pyproject.toml`](https://github.com/nitekeeper/memex/blob/main/pyproject.toml).
- [ ] **Add `.github/workflows/ci.yml`** with three parallel jobs (lint, security, tests). Pin `actions/checkout` and `actions/setup-python` to SHAs from day one (`actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2`, `actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0`). Pattern lives at [memex's `ci.yml`](https://github.com/nitekeeper/memex/blob/main/.github/workflows/ci.yml).
- [ ] **Add `.github/dependabot.yml`** for weekly pip + github-actions updates. Copy from memex.
- [ ] **Run the baseline cleanup once:** `ruff check --fix .` then `ruff format .`, commit as a separate "style:" commit so the gates start from a clean baseline.
- [ ] **Triage remaining Ruff + Bandit findings.** Either fix or `# nosec`/`# noqa` with justification — never blanket-suppress without a reason in the comment.

## When atelier ships its first release

- [ ] **Add `scripts/bump.py` + `.github/workflows/release.yml`** mirroring memex's pattern. Bump script touches `.claude-plugin/plugin.json` + `pyproject.toml` + the dist manifest; release workflow fires on `v<X.Y.Z>` tag push and creates the GitHub Release + dispatches to agora.
- [ ] **Create `AGORA_DISPATCH_TOKEN` secret in this repo.** Same fine-grained PAT used for memex (scoped to `nitekeeper/agora`, Contents Read + Write, Metadata Read; 1-year expiry). Re-paste under *Settings → Secrets and variables → Actions* with the same exact name.
- [ ] **Add the agora dispatch step to `release.yml`** so atelier releases auto-bump the agora pin. Same step memex's `release.yml` has — see [memex#10](https://github.com/nitekeeper/memex/pull/10) for the env-var-passing pattern that avoids the shell-injection class of bugs.
- [ ] **Add atelier to `PLUGIN_REPOS_READ_TOKEN`'s scope** if it's not already — that PAT lives in agora and currently covers `nitekeeper/memex` and `nitekeeper/atelier`. Verify atelier is in the list once atelier becomes a real release-shipping repo.

## When this repo goes public

Same post-public unlocks as memex / agora:

- [ ] Enable branch protection on `main` (`gh api -X PUT repos/nitekeeper/atelier/branches/main/protection ...`).
- [ ] Enable `allow_auto_merge` on the repo (`gh api -X PATCH repos/nitekeeper/atelier -F allow_auto_merge=true`).
- [ ] Enable CodeQL.
- [ ] (Optional) SonarCloud.
