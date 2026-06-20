# TODO

Deferred work tracked here so it survives session boundaries. Cross out items as they land or get explicitly dropped.

## Deterministic-host CLI sandbox prerequisites (per platform)

The deterministic-host engine confines autonomous `claude -p` agents with **Claude Code's native sandbox** (`cli_dispatch.native_sandbox_wrap`, fail-closed). Prerequisites are **per-platform** (verified against code.claude.com/docs/en/sandboxing); availability is detected by `sandbox_runtime_available()` / `sandbox_prereq_status()`:

- **Linux / WSL2** → install **bubblewrap + socat**: `sudo apt install bubblewrap socat` (bwrap = filesystem isolation; socat = the network-proxy relay for `network.allowedDomains`). On Ubuntu 24.04+ also add the AppArmor `bwrap` userns profile (see the doc's accordion).
- **macOS** → **nothing to install**; the built-in Seatbelt framework is used.
- **Native Windows** → unsupported; run atelier under **WSL2** (reports as `linux`, uses the bwrap+socat path).

Without the runtime, the mandatory-sandbox gate (`UnsandboxedRealRunError`) fail-closes and the live e2e harness (`tests/test_e2e_live.py`, `-m live`) skips with a platform-correct reason.

## Wave 0 Task 5 follow-ups (migrations split audit)

Findings from the reviewer + QA audit on commit `47f5b27` (now rebased / amended) that intentionally don't ship with the split itself.

- [ ] **Reviewer Nit-1 — `idx_workspaces_identity` is redundant.** `workspaces.identity` is `UNIQUE NOT NULL`, so SQLite auto-indexes it; the explicit `CREATE INDEX idx_workspaces_identity` (shared schema line 28) duplicates that. Defer to a spec amendment — removing it now risks breaking consumers that drop the index by name. Add a one-line note when the spec is touched. — Deferred pending spec amendment; removing risks breaking consumers that drop the index by name.
- [ ] **Reviewer Nit-3 — `phase_bypasses.agent_id` no FK.** v1.0.13's `005_soft_walls.sql` declared `agent_id TEXT NOT NULL REFERENCES agents(id)`. v1.1.0 widens to `TEXT NOT NULL` so Memex-mode bypasses (where agents live in `~/.memex/agents.db`, not the workspace DB) can still log. The trade-off: audit-trail rows can now reference an agent that doesn't exist anywhere on disk. Reintroduce the FK if/when both modes share an agents source. — Deliberate trade-off: widened from FK to TEXT NOT NULL so Memex-mode bypass rows (agents in ~/.memex/agents.db) can log without a local FK.
- [ ] **Reviewer Nit-4 — `meeting_minutes.filename` is now nullable.** v1.0.13 required it; v1.1.0 makes it optional so DB-only minutes (no `.ai/meetings/*.md` export) are representable. Plan 4's legacy reader must default `NULL → ''` only if downstream code chokes on `None` — leave as `NULL` if callers handle it. — Deliberate: nullable allows DB-only minutes (no .md export). Callers use `meeting.get("filename") or ""` guard.

## When atelier ships its first release

- [ ] **Add atelier to `PLUGIN_REPOS_READ_TOKEN`'s scope** if it's not already — that PAT lives in agora and currently covers `nitekeeper/memex` and `nitekeeper/atelier`. Verify atelier is in the list once atelier becomes a real release-shipping repo. — [CANNOT VERIFY — requires GitHub secrets access]

## When this repo goes public

Atelier went **public on 2026-06-20**. Branch protection on `main` is now enabled (1 required review + the three CI status checks). Remaining post-public unlocks (same set memex / agora got):

- [ ] Enable `allow_auto_merge` on the repo (`gh api -X PATCH repos/nitekeeper/atelier -F allow_auto_merge=true`). — currently `allow_auto_merge=false`.
- [ ] Enable CodeQL. — code-scanning currently `not-configured`.
- [ ] (Optional) SonarCloud.
