# Changelog

## Unreleased

## v1.13.0 — 2026-06-27

A kaizen-driven token-reduction run (#25). Two behaviour-preserving changes that cut
the tokens spent on unread test output and on inert per-spawn protocol; a third
candidate was investigated and abstained as unsafe. Verified by deterministic
before/after benchmarks, two independent reviews, and a green CI (2054 tests).

### Changed
- **Quiet test gate (#25).** Every full-suite `pytest` gate run across the dev-arc
  (`dev-tdd`/`review`/`qa`/`finish`/`receive-review`/`diagnose`/`subagent`/`security`)
  and the host CI-mirror gate (`scripts/self_improve.py`) now runs
  `pytest -q --tb=short` instead of `-v`. The per-test `PASSED` scroll (one line per
  ~2,050 tests) is read by nobody; quiet output is ~80× smaller per run (47,712 → 594
  approx. tokens) while `--tb=short` keeps the FAILURES section + tracebacks intact.
  This aligns agents with atelier's own CI, which already uses `pytest tests/ -q`. The
  four targeted single-test `::test -v` runs (watching one new test execute) are
  preserved. Anti-revert PIN tests lock the gate command.
- **CLI-mode briefing diet (#25).** When `transport == cli` (atelier's only shipped
  transport), `compose_briefing` no longer injects the bridge/heartbeat/shutdown
  protocol it then tells the worker to ignore (a one-shot `claude -p` spawn has no
  inbound channel): the `TM-001..005` rules, the Heartbeat clause, the Agent-Rights
  body (replaced by a one-line auditability note), and the `role.j2` `# CHANNELS`
  bridge table are stripped from the INJECTED copy, and the context-budget reference
  subsection is de-duplicated against the appended rule. Per-spawn briefing ≈24.4k →
  ≈17.2k chars (−7,156, Loom-off). Stripped from the injected copy only — on-disk
  `internal/team-mode-rules/SKILL.md` and `role.j2` are byte-identical (sha256-pinned);
  the load-bearing carveouts (`TM-006/007/008`, the reply contract, the abandon
  grammar) and the Loom-on section are preserved; gated on exact
  `transport == TRANSPORT_CLI` so a future transport safe-degrades to the full briefing.

### Notes
- A third candidate — digesting the host-engine reply echo returned to the driver —
  was **abstained**: that return is shared by programmatic consumers (the outcome
  dict, the review→fix loop, abandonment parsing) and the interactive driver, the
  existing `default_wave_digest` is type-incompatible with the return shape and not
  failure-aware, so the ~2,160-token prize was not worth the blast radius.

## v1.12.0 — 2026-06-26

Two kaizen-driven token-reduction runs against `compose_briefing`. Net effect: the
per-spawn worker briefing shrank ~34% in the common (Loom-off) case (≈36.7k → ≈24.3k
chars for a representative implementer spawn), behaviour-preserving. Every strip
applies to the INJECTED copy only — the on-disk `internal/**/SKILL.md` files are
unchanged, so the file-reading tests and the raw-file `ABANDON_RE` parse in
`pm_dispatch_envelope.py` are unaffected.

### Changed
- **Rules-block trim + de-duplication (#22).** `_read_rules_block` strips the YAML
  frontmatter, the maintainer HTML comment, and the CHANGELOG from the injected
  team-mode rules block. The abandon-grammar and reply-envelope sections —
  previously rendered twice per briefing — are de-duplicated: the `role.j2`
  template is now the canonical reply-contract copy, updated to carry the `failed`
  token, the `attempt` anti-spoofing field, and the field constraints
  (artifacts-emptiness, type/status validity, `notes_md` cap).
- **Loom rules gated on availability (#23).** The `## Loom chat transport` section
  is injected only when Loom is the active chat transport (`team_chat.transport ==
  'loom'`) — the same signal `role.j2` branches on — so the F16/A9
  mandatory-when-available contract holds when Loom is live, while the ~3.2KB
  section is no longer paid on every spawn when Loom is unavailable.
- **Phase-procedure boilerplate trim (#23).** The injected phase procedure drops its
  frontmatter, the Prerequisites mode/required-tables implementation lines, and the
  pre-dispatch "Check the phase gate" step (a soft-wall check the orchestrator runs
  before dispatch — un-executable by a non-interactive `claude -p` worker).
  Implementation steps, the Iron Law, and Hard rules are preserved.
- **`_MINIMAL_DIFF_RULE` trim (#23).** The "REFLEX, NOT RESEARCH" motivational
  paragraph is reduced to its behavioral core; all safety carve-outs
  (WHEN-NOT-TO-BE-LAZY, COVER-EVERY-REQUIREMENT) preserved.

### Fixed
- `_CLI_TRANSPORT_RULE` described "four closure tokens"; corrected to five
  (`done`/`blocked`/`abandoned`/`needs-input`/`failed`), removing a contradictory
  superseding tail that could mis-route a deterministic `failed` result. (#22)

## v1.11.1 — 2026-06-26

### Removed
- **Retired the benchmark suite.** Removed `benchmarks/` (the ponytail/terse
  A/B harness — `run.py`, `bench_fullcycle.py`, fixtures, results, README) and
  its `benchmarks` offline-selftest CI job. The token-lever benchmarking
  initiative is concluded; the *kept* outcomes (the briefing levers and the
  terse-rule removal) remain in the shipped code — only the measurement harness
  is gone. Nothing in the plugin runtime imported `benchmarks/`, so the removal
  is inert. CI now runs lint + security + tests.

## v1.11.0 — 2026-06-26

### Added
- **`/atelier:tokens` — daily token-usage rollup.** A new stdlib-only reader
  (`scripts/token_usage.py`) walks Claude Code's `~/.claude` transcripts and
  emits a per-day × model rollup of the four token categories (input, output,
  cache-creation, cache-read) plus the cache-write TTL split, mirroring the
  verified tokscale dedup contract (`msg.id:requestId`, within-file MAX-merge +
  across-file first-wins, sidechain include + reparent, top-level
  `message.usage`). `scripts/token_pricing.py` adds per-model USD costing
  (cache-write 5m = 1.25× / 1h = 2.0× base input; unknown model → no cost).
  Exposed as the `/atelier:tokens` skill via a `daily` CLI
  (`--config-dir` / `--since` / `--format {json,csv,markdown}` / `--cost`); the
  no-`--cost` JSON is a byte-stable feed (consumed by Loom's token-usage panel).

### Changed
- Sharpened the `_MINIMAL_DIFF_RULE` carve-out to cover **compound tasks**:
  minimality now explicitly applies to the SIZE of a change, never its SCOPE —
  every stated requirement / acceptance criterion must be satisfied ("fewer
  lines per requirement, not fewer requirements"). Closes the completeness gap
  the three-model benchmark localized to multi-requirement tasks (the
  `form-validation` case where the lever dropped a requirement). The new clause
  is pinned un-trimmable in `tests/test_minimal_diff_lever.py`.

### Removed
- Removed the terse/caveman briefing rule (B1) — measured net loss at every tier;
  the B2 wave-digest codec and context-budget rule are retained. The
  `_TERSE_OUTPUT_RULE` constant, the `ATELIER_INCLUDE_TERSE` env, and
  `_terse_rule_applies()` are deleted from `scripts/dispatch.py`. The
  `compose_briefing` / `_host_briefing_for` `include_terse` parameter (which only
  ever gated the always-on context-budget tail after B1 went default-off) is
  renamed to `include_context_budget`; behavior for that tail is byte-identical.

## v1.10.1 — 2026-06-19

**Migration runner self-heals a ledger/schema desync instead of crashing.** A
Memex-mode store whose `migrations` ledger had fallen behind its actual schema
caused the bootstrap runner to re-apply an already-applied migration
(`ALTER TABLE tasks ADD COLUMN parallel_group`) → `duplicate column name`, which
broke `/atelier:save`. `scripts/migrate.py` now applies each migration
**per-statement** (split via stdlib `sqlite3.complete_statement()`), skipping
only an already-exists statement and continuing — so genuinely-new statements
still apply, while every other error still propagates and a file is recorded
only once fully applied. Self-wrapped `BEGIN;…COMMIT;` migrations are honored.

Adds `migrations/shared/014_project_documents_rebuild.sql`: a forward, idempotent
rebuild that drops the orphan `project_documents.type` column and relaxes
`NOT NULL` on `workspace_id`/`project_id`, preserving rows + FTS.

**Child-process reaping fix.** `cli_dispatch.real_cli_runner` now reaps the
entire process group on wall-clock timeout (`start_new_session` +
SIGTERM→grace→SIGKILL via `os.killpg`, then `os.waitpid`), so a hung worker can
no longer orphan a grandchild.

## v1.10.0 — 2026-06-15

**M7 — the bridge dispatch QUEUE is retired and the `bridge_requests` table is
dropped.** The deterministic host/CLI transport (made the default in v1.9.0) is
now the SOLE dispatch path. This is the final code milestone of the v2
deterministic-host migration. The inter-agent message **WIRE**
(`bridge_messages` / `bridge_send` / `bridge_read` / `bridge_payloads`,
`team_meeting`, `status`) is unaffected — only the dispatch queue is removed. The
§17 resume feature is unaffected (it reads `team_audit_log` + `tasks` only).

### Removed
- The bridge dispatch QUEUE: `QueueBridgeDispatchTools`, `build_spawn_fn`,
  `build_poll_fn`, and the `BRIDGE_*` tunables (`scripts/dispatch.py`); the
  `build_wave_dispatcher` / `build_wave_dispatcher_for_project` agent-team
  dispatcher factories (`scripts/atelier_entrypoint.py`); the per-turn servicer
  SKILL `internal/bridge-poll/SKILL.md`.
- The harness-team lifecycle that the queue fed — `scripts/sweep_leaked_teams.py`
  and `scripts/team_teardown.py` — which has no producer once the agent-team
  dispatcher is gone (the `teams` table is never written in production).
- The `bridge_requests` table, via forward-only migration
  `013_drop_bridge_requests.sql`.
- `ATELIER_TRANSPORT=bridge` is no longer accepted — it now raises
  `UnknownTransportError` (the queue it selected is gone; `cli` is the only
  transport).

### Changed
- `scripts/abort.py` is stripped to a transport-agnostic recorder: it writes the
  durable postmortem + the `'aborted'` `team_audit_log` event (the resume signal)
  + applies the worktree policy; it no longer enqueues a `team_delete` row or
  resolves team_id from the queue.
- The Memex bootstrap path now reconciles shared migrations on every run
  (`memex_stores.migrate`) — previously `create_store` applied migrations only on
  first provision, so an existing store would never receive a new migration.
- The dispatch SKILLs (`dev-dispatch`, `dev-finish`, `abort`, `run`,
  `plan-wave-1`, `pm-dispatch`, `status`) are purged of the legacy bridge recipe
  and queue references; the host-drive path is now THE path.

### Fixed
- Four host-path defects found in live validation: the envelope schema `task_id`
  union → single `"string"` (claude `--json-schema` ajv strictTypes); a
  `MIN_BUDGET_USD` floor in `max_budget_usd_for` (tiny derived ceilings aborted
  large tasks); `DEFAULT_ALLOWED_TOOLS` defaulted at the argv sites; and a
  per-writer worktree sandbox (`native_sandbox_wrap(write_root=…)`) so a writer's
  output is confined to (and lands in) its own worktree.
- The engine-level false-`done` guard no longer mis-fires on a **journal
  replay** (a replayed `done` does not re-write its already-merged file); it is
  skipped on a journal hit, and a rejected fresh `done` now invalidates its
  journal entry (`ResultJournal.delete`) so a retry re-executes instead of
  replaying the rejected result.

### Added
- `tests/test_no_bridge_residue.py` — a guard that fails loud if any deleted
  queue/dispatcher/lifecycle symbol returns to the live source, asserts
  `bridge_requests` appears only in migration history, and proves at the DB level
  that a full migration run leaves no `bridge_requests` table.

### Housekeeping
- Clarifying comments on the `model_tier` `('review','opus')` ROLE_FLOOR
  (intentional future-proofing; floored via `PHASE_TIER` today) and a stale
  `bridge_send.py` TODO comment; a new `abort.py` missing-`teams`-row regression
  test; `TODO.md` reconciled (R-MODE marked delivered).

## v1.9.0 — 2026-06-14

**Settings recommendation is now a two-profile choice.** The first-session,
once-per-version settings offer is upgraded from a binary `y/N` to a
NAMED-PROFILE menu — `cost-effective` (the recommended default) vs
`code-quality` — plus an explicit skip. Each profile now also recommends a
**subagent model** via the `CLAUDE_CODE_SUBAGENT_MODEL` env key. **Pressing Enter
(an empty answer) APPLIES the recommended `cost-effective` profile** (informed
consent — the menu states that Enter writes the posture to
`~/.claude/settings.json`); an explicit skip (`s`/`skip`/`n`) writes nothing, and
an unrecognized non-empty typo re-asks once rather than writing.

### Added
- **Two named settings profiles** in `scripts/recommended_settings.py`:
  `cost-effective` (orchestrator `model: sonnet` @ `effortLevel: high`, subagents
  `haiku`, `autoCompactEnabled`) and `code-quality` (orchestrator `model: opus`
  with `ultracode: true` ⇒ xhigh effort, subagents `sonnet`, `autoCompactEnabled`).
  Exposed as `PROFILES` / `DEFAULT_PROFILE`; all values are version-resilient
  family aliases.
- **Subagent-model recommendation** via the top-level
  `env.CLAUDE_CODE_SUBAGENT_MODEL` key (`scripts/recommended_settings.py`). The
  writer nested-merges `env`, preserving unmanaged env keys (e.g.
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`).
- **Managed-key reconciliation** (`scripts/recommended_settings.py`):
  `apply_profile(profile_id)` sets the chosen profile's managed keys and CLEARS
  the stale mutually-exclusive key (applying `cost-effective` removes a stale
  `ultracode`; applying `code-quality` removes a stale `effortLevel`).

### Changed
- **`internal/settings-recommendation/SKILL.md`** — the binary `[y/N]` block is
  replaced by a profile MENU (cost-effective marked recommended, code-quality,
  explicit skip). **Enter / empty ⇒ apply `cost-effective`** (the reversed
  consent default, with informed-consent wording); explicit skip ⇒ write nothing;
  unrecognized non-empty input ⇒ re-ask once. `apply_profile(<id>)` +
  `write_state(version, <id>|'declined')` wiring.
- **`scripts/recommended_settings.py`** — `compute_changes(current, profile)` now
  returns a per-profile diff (`set`/`env_set`/`remove`/`empty`); `maybe_offer()`
  carries `default_profile` + a per-profile `profiles` map; `write_state` accepts
  the profile ids ∪ `declined` (and tolerates OLD-format state files on read).
  `apply_recommended()` is kept as a thin back-compat wrapper for the default
  profile.
- **`CLAUDE.md`** (`## Model recommendations`) — documents the two profiles, the
  `CLAUDE_CODE_SUBAGENT_MODEL` mechanism, and that **subagent EFFORT is not
  independently controllable** by the harness (subagent control is MODEL-ONLY).

## v1.8.0 — 2026-06-12

**Loom agent-chat comms mandatory when available.** Loom inter-agent chat is
upgraded from default-when-available to **MANDATORY when Loom is available**,
in BOTH team mode and subagent mode. The single opt-out is the operator env
var `ATELIER_LOOM_COMMS=0` (`"0"` is the only disabling value, gated inside
`loom_comms.detect()` — the single availability choke point). Agents now
follow a deregister-on-completion / rejoin-on-demand lifecycle. Everything
stays fail-soft: Loom failures never block or abort a cycle, Loom never
replaces the mandatory bridge `task_result` completion reply, and when Loom
is unavailable (or opted out) behavior is byte-identical to bridge-only.

### Added
- **`ATELIER_LOOM_COMMS` opt-out env var** (`scripts/loom_comms.py`). Checked
  inside `detect()` so one gate covers team mode, subagent mode, and every
  orchestration helper; `"0"` is the only disabling value.
- **`loom_comms.rejoin()`** — returns a previously-deregistered agent to the
  cycle channel on re-engagement. Join-first; a stale-session join failure
  (non-zero exit, surfaced via the new `_run_loom_raw()`) re-registers then
  re-joins. Fail-soft, idempotent; reports `rejoined` / `reregistered` flags
  plus the `assigned_name` it joined as — on a collision rename the agent
  joins AS the server-renamed identity, and the orchestrator must use the
  returned `assigned_name` for subsequent directed sends.
- **Teardown collision-suffix sweep** (`TEARDOWN_COLLISION_SWEEP_MAX = 4`).
  `teardown()` now also deregisters the deterministic collision-suffixed
  variants `<name>-2` .. `<name>-4` left behind when an agent re-registered
  after a stale session.

### Changed
- **Posture: MANDATORY when available** across the dispatch choke points —
  `internal/dev-dispatch/SKILL.md` (team mode, step 3b),
  `internal/dev-subagent/SKILL.md` (subagent mode, step 2a + `loom_section`
  injection per briefing), and the worker contract in
  `internal/team-mode-rules/SKILL.md` (v1.3.2): when Loom is detected,
  conversational comms MUST ride Loom; the briefing's Loom block is no longer
  an option to skip.
- **Deregister on completion / rejoin on demand.** Workers self-`deregister`
  as their final Loom action at terminal closure; the orchestrator's
  end-of-cycle `teardown()` sweep is the guaranteed backstop, not a
  substitute; re-engaged agents come back via `rejoin()`.

### Docs / Ops
- Operational rule **A9** (mandatory loom-agent-chat comms) added to
  `CLAUDE.md`; `## Inter-agent communication transport` and the README's
  inter-agent chat section updated to the mandatory-when-available posture.

### Fixed
- **Memex >= 2.10 broke every Memex-mode write** (`ImportError: cannot
  import name 'stores' from 'scripts'` /
  `ModuleNotFoundError: No module named 'scripts.db'`). Memex's
  `agents/librarian.py` and `reference_librarian.py` gained module-level
  `from scripts import stores / agents / embeddings` not covered by
  `_scripts_db_shim`, and Memex defers some `scripts.*` imports into
  function bodies that run after the exec-scoped shim exits
  (`embeddings.log_skip`, `embeddings._record_model_info`,
  `db.require_bootstrap`). `_SHIM_BOOTSTRAP` is now a tiered dependency
  map over all six Memex modules; the new `_expose_scripts_modules`
  injects them under both `sys.modules` AND the `scripts` package
  attributes (so `from scripts import agents` can no longer silently bind
  Atelier's own `scripts/agents.py`); the new `_memex_call_shim`
  re-activates the shim around every call into loaded Memex modules so
  deferred imports resolve. All Memex-boundary call sites outside
  `backend_memex.py` (`roles`, `workflow`, `session`, `agents`,
  `meetings`) now route through facade helpers, including the new
  SELECT-only `_memex_core_raw_query`; `meetings`' legacy
  `_ensure_memex_importable` pattern was dead-on-arrival in Memex mode
  and is fixed. 15 regression tests pin every diagnosed mechanism.

## v1.7.0 — 2026-06-08

**Loom agent-chat in subagent mode.** The `dev-subagent` execution path now
participates in Loom inter-agent chat on the same availability-gated basis as
`dev-dispatch` (team mode). When Loom is running, the coordinator opens a
channel before the first task, passes runnable Loom commands into each
subagent briefing, and sweeps the channel clean at the end. When Loom is
unavailable the session is byte-identical to before — bridge-only fallback is
unchanged.

### Added
- **Loom kickoff in `dev-subagent`** (`internal/dev-subagent/SKILL.md` step 2a).
  `detect()` + `kickoff()` before first task dispatch; `build_team_chat_context()`
  per subagent with `loom_section` injected into each briefing; `teardown()` after
  all tasks complete. (#111)
- **`{{loom_section}}` placeholder** in all three briefing templates
  (`implementer-prompt.md`, `spec-reviewer-prompt.md`, `quality-reviewer-prompt.md`).
  Expands to a Loom command table when available, empty string when not. (#111)

## v1.6.0 — 2026-06-08

**Auto-register hooks + Memex scope gate.** Atelier now wires its PostToolUse
and PreCompact hooks into `~/.claude/settings.json` automatically on every
session start (idempotent; upgrades update the path in-place). In Memex mode,
`.ai/active_project` is written at session open so the hook scope gate fires
correctly — previously hooks silently bailed out for all Memex sessions.

### Added
- **Hook auto-registration** (`scripts/bootstrap.py::_register_hooks`). Merges
  `hooks/hooks.json` into `~/.claude/settings.json` on every `run_bootstrap()`
  call. Matched by script filename so re-runs after a version upgrade update
  the path rather than duplicating the entry. Atomic write (temp + `os.replace`)
  so readers never see a partial file. (#110)
- **Memex scope gate fix** (`scripts/atelier_entrypoint.py::_write_active_project_file`).
  Writes sentinel `"memex"` to `.ai/active_project` at the start of every
  `proceed-memex` branch so `hooks/context_budget.py` and `hooks/pre_compact.py`
  know an Atelier session is active. Previously these hooks always bailed out
  silently in Memex mode. (#110)

## v1.3.0 — 2026-06-01

**Team-mode: real multi-agent cycle execution.** This release lands the
team-mode epic (#39) — atelier can now dispatch a wave-orchestrated team of
role personas over a live tmux workspace, in addition to the existing
single-sub-agent path — plus workspace-scoping groundwork and the v1.2
deferred surfaces.

### Added
- **Team-mode epic (#39).** Mode selection in `/atelier:run`, tmux prereq +
  config/layout management with pane-state labels, PM wave-dispatch engine
  (wave barrier + envelope validator + stall/attempt budget), DAG validation
  gates (`scripts/dag.py`), plan-phase planner (wave-0 specialists + wave-1
  synthesis) with reviewer-disjointness enforcement, production dispatch
  binding over a live queue-bridge transport, agent-team behaviors
  (plan-phase meeting, side-query, runtime roster-extension), lifecycle
  skills `/atelier:abort` + `/atelier:status`, `sweep_leaked_teams`, and
  read-first stall detection (GO-OBSERVE before hard-kill / wall-clock
  abandon). Refs #57–#90, #94.
- **Workspace scoping.** `scripts/scope.py` (`resolve_scope()` +
  `~/.atelier/state.json` helpers), workspace/project/document CRUD stubs,
  workspace-less operations (migration 005), and multi-workspace integration
  tests. Refs #50–#56.
- **v1.2 deferred surfaces** (#30, #33, #35) and reintroduced
  `tasks.parallel_group` column (#34).

### Changed
- Backend write surface extended: `update_task`, `delete_task`,
  `assign_task`, `list_tasks` filter; downstream call-site wiring in
  `projects.py` / `documents.py`; `_WORKSPACE_SLUG` hardcoding removed and
  the `workspace_id` filter activated. Refs #31, #54, #55.

### Docs / Ops
- Consolidated operational rules (A1–A8) into `CLAUDE.md` (#41), default +
  per-skill/role model recommendations (#42), and untracked process
  artifacts with the Memex/Notion storage policy (#40).

### Internal
- Migrations 003/005/009, bridge + dispatch foundationals, dev-finish
  team-mode + resume support, and dependency bumps (jinja2, libtmux, pyyaml,
  pytest, pytest-mock).

### Migration notes
- No destructive schema changes. Team-mode requires `tmux` + `libtmux
  >=0.58.0` only when the team-mode path is selected; the single-sub-agent
  path is unaffected.

---

## v1.2.0 — 2026-05-20

**Memex-mode bug fixes + bootstrap wiring.** Resolves a class of
`workspace_id`-related crashes that surfaced once atelier started writing
through Memex v2 in production, and wires atelier's bootstrap into
memex's new `ensure_internal_agents()` invariant.

**Memex compatibility:** Best-effort soft dependency on Memex **v2.6.0+**
(for `ensure_internal_agents()`). Older memex versions log a warning and
continue — no crash, no behaviour change beyond the dropped invariant
restore. Memex v2.2.0+ API floor from v1.1.0 is unchanged.

### Added
- `scripts/bootstrap.py:_run_bootstrap_memex` now calls memex's new
  `ensure_internal_agents()` API after seeding atelier's roster into
  `~/.memex/agents.db`, restoring memex's internal-agent invariant
  after each touch of the shared `agents.db`. Soft-imported — older
  memex versions (pre-2.6.0) log a warning and continue. Refs: #9, #12.

### Fixed
- **memex-mode `workspace_id` propagation across 4 write paths.**
  `scripts/projects.py:_resolve_workspace_id` was queried
  unconditionally against `backend_local`, crashing in memex mode with
  `OperationalError: no such table: workspaces`. `backend_memex`'s
  `upsert_session` / `write_document` / `write_meeting` built INSERT
  payloads omitting `workspace_id` while the target tables
  (`sessions`, `project_documents`, `meeting_minutes`) declare
  `workspace_id NOT NULL` — the first memex-mode write to any of these
  would crash with `IntegrityError`. All four paths are now
  mode-aware and inject `workspace_id` correctly. Refs: #6 bugs 1–2
  (+ latent 3–4), #8.

### Internal
- Extracted shared `workspace_resolution` module to eliminate the
  duplicated `_resolve_singleton_workspace_id` helpers that PR #8
  introduced across four scripts. Refs: #10, #11.
- `_atelier_version()` fallback sentinel bumped `1.1.0` → `1.2.0`
  (the v1.1.1 release missed this bump; caught up here).

### Migration notes
- None — pure bug fixes; existing API surface unchanged. Memex
  callers benefit automatically once memex is upgraded to v2.6.0+.

## v1.1.1 — 2026-05-18

### Fixed
- `_scripts_db_shim` no longer recurses into `_load_memex_module`
  when memex imports back into `scripts.*` during bootstrap — the
  shim's reentrancy guard now short-circuits on second entry.
- Documentation and lint guard: replaced bare `python` invocations
  with `python3` in docs; added a lint guard to keep them out of
  future docs.
- Replaced `try/except/pass` with `contextlib.suppress` per ruff
  SIM105.

## v1.1.0 — 2026-05-18

**Memex v2 integration.** Atelier now writes through Memex v2 when
installed, with a slim project-local fallback otherwise.

**Memex compatibility:** Requires Memex **v2.2.0+** (API floor —
caller-built `librarian_output` landed in v2.2.0). Strongly recommended:
**v2.5.0+** (auto-bootstrap eliminates manual `python -m scripts.install`),
**v2.5.1+** (atelier can drop client-side `__*` namespace filtering).
Bootstrap refuses to run against Memex installs older than v2.2.0.

**Typed exceptions surfaced by memex.** Atelier callers may now see the
following typed exceptions propagated from memex:

- `librarian.DuplicateKeyError` — raised on key collision during
  `write_entry` (memex v2.3.0). Atelier's migration replay handles this
  via a client-side Index lookup before every write.
- `embeddings.EmbeddingUnavailable` — raised when embeddings can't be
  produced (oversized input, missing API key, provider error) (memex
  v2.4.1). Atelier surfaces the reason and falls back to FTS-only.
- `data_steward.OrphanNotFoundError` — raised when attempting to operate
  on an `index_id` that isn't present in the documents table (memex
  v2.4.0).
- `db.MemexNotInitializedError` — raised when `~/.memex/registry.json`
  is missing (memex v2.5.0). Atelier's `migrate_to_memex` catches and
  re-raises with operator guidance ("Run `memex:run` once before
  migrating").
- `db.MemexHomeInvalidError` — raised when `MEMEX_HOME` is set to an
  invalid path (memex v2.5.0).

### Added
- Dual-mode persistence facade (`scripts/backend.py`) — auto-selects
  between Memex Core and project-local SQLite.
- `scripts/backend_memex.py` — Tier 2 writes through
  `librarian.write_entry()` with caller-built `librarian_output` (no LLM
  dispatch for Atelier's structured domains); Tier 1 state mutations via
  Memex Core direct.
- `scripts/backend_local.py` — slim SQLite with FTS5 over a local
  `documents` table; raw bodies archived to `.ai/raw/`.
- `scripts/bootstrap.py` — idempotent Memex-mode bootstrap (seeds
  Atelier roles + shipped agents into `~/.memex/agents.db`; creates
  the `atelier` store; enforces Memex v2.2.0+ API floor; piggybacks
  on memex v2.5.0+ auto-bootstrap when available).
- `scripts/migrate_to_memex.py` — one-shot per-project replay from
  Local to Memex; crash-safe (no marker without full success).
- `scripts/atelier_entrypoint.py:startup_check()` — pre-flight for the
  four pre-existing user-facing skills (load, save, ingest, run); handles
  bootstrap + migration prompt. `/atelier:migrate` is excluded from
  pre-flight to avoid circular logic (it IS the migration path).
- `scripts/domain_vocabulary.py` — fixed Atelier domain set
  (`project` / `task` / `meeting` / `project_doc` / `adr`); validated
  on every Tier 2 write.
- `templates/roles.json` + `templates/agents/*.json` — Atelier-shipped
  role + agent seed data, used by both modes.
- `migrations/shared/` + `migrations/local-only/` — split so Memex mode
  consumes only schema-without-roles-or-agents (Memex's agents.db
  owns those tables). `migrations/shared/006_index_ids.sql` adds
  `index_id` columns required by `librarian.write_entry`.
- 8 new internal procedures under `internal/{memex,local,bootstrap-memex,
  migrate-local-to-memex}/` plus `internal/memex/domain-vocabulary.md`.

### Changed
- `scripts/{projects,tasks,documents,meetings,session,workflow,roles,
  agents}.py` rewired to call `backend.*` instead of opening SQLite
  directly. Public signatures unchanged.
- `CLAUDE.md` no longer requires Memex to be installed.

### Removed
- `scripts/db.py` — module's only consumer (the connection helper) is
  now inline in `scripts/migrate.py`.
- `.ai/memex.db` hard-dependency check.

## v0.2.0 — 2026-05-15

### Added
- `skills/using-atelier/SKILL.md` — canonical methodology source (trigger contract, Red Flags, phase guidance, dev arc, bypass procedure).
- `hooks/session_start.py` — SessionStart hook injecting `using-atelier` body as system context.
- `templates/CLAUDE-snippet.md` — backup methodology snippet for consumer projects' CLAUDE.md.
- `phase_bypasses` table and `workflow.py log-bypass` CLI subcommand for auditing soft-wall bypasses.
- YAML frontmatter with `description: Use when…` on `using-atelier`, `ingest`, `save`, `load`.
- `dev:handoff` retro now surfaces phase bypasses from `phase_bypasses` table.
- Migration `005_soft_walls.sql`.

### Changed
- `workflow.py check_gate` now returns a `GateResult` dataclass instead of raising `WorkflowError` on phase mismatch. Out-of-phase invocations no longer block — skills ask the user to confirm a bypass.
- CLI `workflow.py check-gate` now outputs JSON (`{"allowed", "current_phase", "required_phase", "reason"}`) and always exits 0. **Breaking change**: scripts using shell exit code to detect gate-not-met must migrate to parsing the JSON `allowed` field.
- All dev skills' (`dev:design`, `dev:plan`, `dev:tdd`, `dev:review`, `dev:security`, `dev:qa`, `dev:diagnose`, `dev:handoff`) step 1 updated for the new JSON-based `check-gate` contract and (where walled) the bypass flow. Note: skills shipped as one file per concern (`dev:tdd` is one skill handling all three TDD states red/green/refactor, etc.) — the v0.1.0 CHANGELOG entry listing them separately reflected an earlier naming intent.
- `hooks/session_open.py` now appends phase-specific guidance derived from `using-atelier/SKILL.md`'s phase guidance table.

### Deprecated
- `WorkflowError` raise behavior in `check_gate` on phase mismatch. The exception class itself remains for `workflow.py advance` invalid-transition errors and for `check_gate` invalid-project-id errors.

### Migration notes
- Run `python scripts/migrate.py .ai/memex.db` to apply migration 005.
- **Note:** Atelier scripts currently default to two different DB paths (`workflow.py`/`session.py` use `.ai/atelier.db`; CRUD scripts and `migrate.py` use `.ai/memex.db`). This inconsistency predates v0.2.0 and is tracked as a follow-up cleanup. Ensure both paths are migrated if your project has both.
- Install the SessionStart hook per README "Auto-trigger setup" section.
- (Optional) paste `templates/CLAUDE-snippet.md` into your project's `CLAUDE.md`.

## Unreleased (historical — pre-v0.2.0 entry, heading retained for audit trail)

### Added
- `dev:self-improve` skill — autonomous multi-agent improvement cycle with isolated git clone, unanimous consensus gate, destructive-change detection, and full test gate before merge
- `scripts/destructive_check.py` — detects destructive changes in git diffs (5 categories: deleted imported files, removed public functions, destructive DB migrations, removed skill directories, removed test files)
- `scripts/self_improve.py` — git infrastructure for self-improve cycles (clone, branch, test, commit, push-merge, cleanup, pull)

## v0.1.1 — 2026-05-12

### Fixed

- `migrations/` directory now included in `dist/` — was missing from v0.1.0, causing `migrate.py` to silently skip all SQL migrations when run from `dist/`

## v0.1.0 — 2026-05-12

### Added

- **Foundation** — SQLite database, migration runner, session management, shared pytest conftest
- **Coordination layer** — `scripts/roles.py`, `scripts/projects.py`, `scripts/tasks.py` with full CRUD and search; `skills/role`, `skills/project`, `skills/task`, `skills/meeting`, `skills/doc`, `skills/ingest`, `skills/load`, `skills/save` (22 SKILL.md files total)
- **Workspace layer** — `scripts/workspace.py` for tmux session management (workspaces, rooms, agent desks); `skills/workspace`, `skills/room`, `skills/agent-desk`, `skills/agent`
- **Dev workflow** — `scripts/workflow.py` with phase state machine (design → approved → in-progress → code-review → done / diagnose); `skills/dev-design`, `skills/dev-plan`, `skills/dev-tdd-red`, `skills/dev-tdd-green`, `skills/dev-tdd-refactor`, `skills/dev-code-review`, `skills/dev-qa-review`, `skills/dev-security-review`, `skills/dev-handoff`, `skills/dev-diagnose`
- **Cross-platform workspace** — `scripts/preflight.py` with platform detection, WSL check, and tmux auto-install; Windows routes workspace commands through WSL subprocess (`wsl -- tmux ...`); macOS/Linux use libtmux directly
- **108 tests** across all modules; `tests/conftest.py` for safe CI imports
