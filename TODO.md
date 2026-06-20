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

- [ ] **Reviewer Nit-1 — `idx_workspaces_identity` is redundant.** `workspaces.identity` is `UNIQUE NOT NULL`, so SQLite auto-indexes it; the explicit `CREATE INDEX idx_workspaces_identity` (shared schema line 39) duplicates that. Defer to a spec amendment — removing it now risks breaking consumers that drop the index by name. Add a one-line note when the spec is touched. — Deferred pending spec amendment; removing risks breaking consumers that drop the index by name.

## When this repo goes public

Atelier went **public on 2026-06-20**. Branch protection on `main` is now enabled (1 required review + the three CI status checks). Remaining post-public unlock:

- [ ] Enable CodeQL. — code-scanning currently `not-configured`.
