# Agora — Custom Plugin Marketplace

**Date captured:** 2026-05-15
**Status:** Design discussion complete; implementation not yet started.

## Goal

A local Claude Code plugin marketplace called **agora** that works like a real package manager (apt/brew/npm style). Each plugin lives in its own git repo with semver releases. A separate agora repo holds a curated index of plugin pointers and provides setup + maintenance tooling. Greek `agora` = public marketplace and gathering place, matching the memex/atelier convention of naming a place rather than a function.

## Core model

- **Plugin repo** (one per plugin, e.g. atelier, memex): owns its own code, `plugin.json`, and release tags. Independent git repos.
- **Agora repo** (one): holds `plugins.json` (human-friendly registry) and an `agora` skill that translates `plugins.json` into Claude Code's required `marketplace.json`. Version-controlled.
- **Claude Code**: the client. Reads the generated `marketplace.json` via its native `/plugins > Marketplaces` UI and handles browse / search / install / enable / disable.

## Plugin naming

- Format: `<owner>-<repo>` (single dash separator)
- Example: `github.com/nitekeeper/atelier.git` → plugin name `nitekeeper-atelier`
- Eliminates collisions across owners. Round-trip parsing is not required — the source git URL is stored as the real anchor in `plugins.json`.

## Source pinning

Every entry pins a specific release:

```json
{
  "source": {
    "source": "url",
    "url": "https://github.com/<owner>/<repo>.git",
    "ref": "v1.2.0",
    "sha": "<commit SHA>"
  }
}
```

The SHA gives integrity (retag → install fails). Pre-release tags are skipped by default in update operations.

## Workflows

### Plugin author

1. Build plugin in own repo. Ensure `LICENSE` file is present and GitHub repo description is set.
2. Tag a release (semver): `git tag v1.0.0 && git push --tags`.
3. Clone the agora repo, `cd` into it (or into your plugin repo).
4. Run `agora:plugin-register` (no arguments if run from inside the plugin repo — helper reads `git remote get-url origin`. Optional `--url <git-url>` arg for registering remote / others' plugins).
5. The helper derives every field from the repo — author provides nothing else by hand unless prompted.
6. Commit / PR the change to the agora repo.

One idempotent command serves both first-time register and version-bump update.

#### Field derivation (single source of truth: the git repo)

| `plugins.json` field | Source | Failure behavior |
|---|---|---|
| `name` | URL path → `<owner>-<repo>` | Hard error if URL malformed |
| `repository_url` | The URL itself | Cannot fail |
| `current_version` | Latest semver git tag (skip pre-releases) | Hard error if no release tags |
| `current_sha` | Resolve tag to commit SHA | Cannot fail if version exists |
| `description` | GitHub repo's `description` field (via GH API) | **Prompt author** for a one-line description |
| `license` | LICENSE file (SPDX-parsed) or GH API's `license.spdx_id` | **Hard error** — author must add LICENSE file and re-tag |
| `category` | GH topics mapped against allowed taxonomy; uses first match | Silent omit |
| `keywords` | GH topics array (sans the one used for category) | Silent omit (empty array) |
| `author` | URL owner; optionally enriched via GH API | Never fails |
| `homepage` | GH repo's `homepage` field if set, else the GH repo URL | Never fails |
| `registered_at` | ISO timestamp at first registration | Cannot fail |
| `updated_at` | ISO timestamp at every refresh | Cannot fail |

`plugin.json` is **never read by agora**. It remains the plugin's own runtime config that Claude Code reads after install. Authors don't maintain anything agora-specific.

GitHub API is a hard dependency. All Claude Code plugins are assumed GitHub-hosted.

### Marketplace consumer

1. Clone the agora repo to local machine.
2. Run `agora:setup`. This:
   1. Registers the marketplace in `~/.claude/settings.json` under `extraKnownMarketplaces`.
   2. Compiles `plugins.json` → `.claude-plugin/marketplace.json`.
   3. Validates the registry (every git URL reachable, every `ref` exists, every `sha` matches).
   4. Installs a session-start hook for update announcements.
3. Open Claude Code → `/plugins > Marketplaces > agora` → browse and install.
4. Periodically run `agora:update <name>` or `agora:update --all` to upgrade installed plugins to newer releases.

### Updates

- **User-initiated only.** No auto-upgrade (avoids breaking changes mid-session, dependency churn, stability surprises).
- **Session-start banner** announces pending updates: `atelier  v1.2.0 → v1.3.0`. Quiet line, dismissible.
- Banner reads from a local cache file populated by `agora:check`. Cache TTL ~24h; offline → use last known cache silently.
- `agora:check` forces a refresh.

## Skill operations

| Command | Audience | Purpose |
|---|---|---|
| `agora:setup` | Consumer | One-time machine setup (register marketplace, compile, validate, install hook) |
| `agora:compile` | Maintainer/Author | Translate `plugins.json` → `marketplace.json` after any edit |
| `agora:update <name>` | Consumer | Upgrade one plugin to latest release |
| `agora:update --all` | Consumer | Upgrade all plugins |
| `agora:check` | Consumer | Refresh "available versions" cache |
| `agora:plugin-register <git-url>` | Author | Add or refresh an entry in `plugins.json` |

Claude Code's native `/plugins` UI handles browse / search / install / enable — the skill does not duplicate those.

## File layout (agora repo)

```
agora/
  .claude-plugin/
    marketplace.json       # generated; commit decision TBD
  plugins.json             # human-edited registry, source of truth
  scripts/
    setup.py
    compile.py
    update.py
    check.py
    plugin_register.py
  hooks/
    session_start.py       # update-available banner
  skills/
    agora/
      SKILL.md
  README.md                # contains "Register your plugin" author guide
```

## plugins.json schema

```json
{
  "$schema": "https://nitekeeper.github.io/agora/plugins.schema.json",
  "marketplace": {
    "name": "agora",
    "description": "Custom Claude Code plugin marketplace",
    "owner": { "name": "nitekeeper" }
  },
  "plugins": [
    {
      "name": "nitekeeper-atelier",
      "repository_url": "https://github.com/nitekeeper/atelier.git",
      "current_version": "v1.0.0",
      "current_sha": "abc123...",
      "description": "Shared workspace methodology and dev workflow for human-AI development teams",
      "license": "MIT",
      "category": "development",
      "keywords": ["workflow", "tdd", "skills"],
      "author": { "name": "nitekeeper" },
      "homepage": "https://github.com/nitekeeper/atelier",
      "registered_at": "2026-05-15T19:00:00Z",
      "updated_at": "2026-05-15T19:00:00Z"
    }
  ]
}
```

**Required per-plugin:** `name`, `repository_url`, `current_version`, `current_sha`, `description`, `license`.
**Optional:** `category`, `keywords`, `author`, `homepage`, `registered_at`, `updated_at`.

Denormalized: full metadata stored, refreshed by `agora:plugin-register` on every release bump. Compile (plugins.json → marketplace.json) is fully offline; no network needed.

## Pre-release tag policy

Locked: **stable only by default.** "Latest release" means the highest stable SemVer tag. Tags with pre-release identifiers (`v1.2.0-rc1`, `v2.0.0-beta.3`, `v0.1.0-alpha`) are ignored unless `--include-prerelease` is passed.

Applies to `plugin-register`, `update`, and `check`. Plugin with only pre-release tags fails register with "no stable release found; add a stable tag or use --include-prerelease."

Matches npm / cargo / pip default behavior.

## Open items to settle when building

1. Decide whether `.claude-plugin/marketplace.json` is committed to the agora repo or generated as a gitignored build artifact. (task 15)
2. Define the contribution policy for `plugins.json` edits (direct push vs PR review).
3. `agora:setup` behavior when `~/.claude/settings.json` already has `extraKnownMarketplaces` entries — merge, warn, or refuse.
4. Pre-release tag handling — default-skip, with a `--include-prerelease` flag for `update`.
5. Build the SPDX-license parser (regex or library; e.g., simple first-line keyword matching against MIT/Apache-2.0/etc.).
6. Build the category-mapping table (allowed Claude Code categories ↔ GH topic vocabulary).
7. Accept the release-discipline tradeoff: iteration is slower (commit → tag → push → register/update in agora → commit/push agora → reload plugin in Claude Code).

## Migration impact on existing setup

- Current `~/.claude/settings.json` has `extraKnownMarketplaces.atelier` pointing at `C:/Users/user/Documents/Skills/atelier` (the atelier repo treated as a single-plugin marketplace). This is the configuration that caused the "source type not supported" error earlier today.
- Under the new model, that entry is removed. Atelier becomes just one plugin in agora's `plugins.json`.
- The `marketplace.json` we recently added to atelier's `.claude-plugin/` (commit `5f351b9` on main) can stay or be removed — it is irrelevant under the new model since the atelier repo will no longer be registered as a marketplace.
