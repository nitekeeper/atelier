# tmux agent-state indicator (atelier team-mode panes)

How Atelier's agent-team tmux panes signal per-pane agent state — a zero-dependency composite `pane-border-format` render that always works, plus an OPTIONAL detect-and-source integration of the third-party [`accessd/tmux-agent-indicator`](https://github.com/accessd/tmux-agent-indicator) plugin for a richer three-state indicator. **Atelier never installs the plugin and never writes your global config** (`~/.claude/settings.json`, `~/.tmux.conf` beyond a single `source-file` include line).

This is the atelier#79 port of the patterns kaizen validated first (kaizen#76/#68/#64 composite render + `@desired_title`; kaizen#79 detect-and-source).

## The two layers

| Layer | Source | States | Dependencies | When it applies |
|---|---|---|---|---|
| **Composite border render** (baseline) | `scripts/tmux_setup.py` `CONFIG_BLOCK` + `scripts/tmux_layout.py` `set_pane_title` | 2 (idle / busy via the OSC-2 glyph) + the wave/role label | none — built into atelier's block | **always** (unconditional fallback) |
| **tmux-agent-indicator plugin** (optional) | `scripts/tmux_setup.py` `if-shell -b` guard | 3 (running / needs-input / done) | operator installs the plugin + its CC hooks | only when `~/.tmux/plugins/tmux-agent-indicator` is present |

### Layer 1 — composite `pane-border-format` (zero-dependency baseline)

Atelier's `CONFIG_BLOCK` sets, **unconditionally**:

```
set -g pane-border-status top
set -g pane-border-format '#{=1:pane_title} #[fg=cyan]#{?@desired_title,#{@desired_title},#{pane_title}}#[default]'
```

This composes **two** signals on every pane border:

- `#{=1:pane_title}` — the leading char of `pane_title`, which carries Claude Code's OSC-2 activity glyph (idle/busy).
- `#{?@desired_title,#{@desired_title},#{pane_title}}` — Atelier's own per-pane `@desired_title` user-option (the wave/role label), falling back to `pane_title` for un-tagged panes.

**Why a user-option and not `select-pane -T`:** tmux `allow-rename off` does **not** gate OSC-2 pane titles — Claude Code rewrites `pane_title` (and thus anything set via `select-pane -T`) on every turn. A per-pane **user-option** is immune to OSC-2, so the wave/role label survives. Atelier populates it via `scripts/tmux_layout.py`:

- `set_pane_title(pane_id, label, *, mode=...)` → `tmux set-option -p -t <pane_id> @desired_title <label>` (best-effort; `#` → `##` escaped; never raises).
- `set_pane_titles({pane_id: label, ...})` — bulk form.
- `apply_layout(n_workers, *, mode=..., labels={...})` — applies the PM-1/3 + workers-2/3 geometry, then sets the labels right after, so each label appears **at pane creation**, composing with the layout.

### Layer 2 — optional `accessd/tmux-agent-indicator` (richer 3-state)

The plugin surfaces a three-state per-pane indicator:

| State | Meaning | How it shows |
|---|---|---|
| **running** | the agent is working | pane border color + a Knight Rider animation; status-bar icon |
| **needs-input** | the agent is waiting on you (e.g. a permission prompt) | `needs-input` border (default `yellow`); window-title styling; status-bar icon |
| **done** | the agent finished its turn | `done` border (default `green`); `done` window-title bg; status-bar icon |

States reset on pane focus or on the next transition. Requirements: **tmux 3.0+ and bash 4+**. The plugin supports Claude Code (via hooks), Codex, and OpenCode; **Atelier's scope is Claude Code only**.

## Install (operator step)

**Atelier does NOT install this plugin and does NOT write your `~/.claude/settings.json` or `~/.tmux.conf`.** You install it yourself; the installer wires the Claude Code hooks. Two upstream-supported paths:

One-command (README-recommended):

```
curl -fsSL https://raw.githubusercontent.com/accessd/tmux-agent-indicator/main/install.sh | bash
```

This installs to `~/.tmux/plugins/tmux-agent-indicator` and wires the Claude Code hooks into `~/.claude/settings.json` (it also touches `~/.codex/config.toml` and `~/.config/opencode/plugins/`, which are irrelevant to Atelier).

Or via TPM:

```
set -g @plugin 'accessd/tmux-agent-indicator'
tmux source-file ~/.tmux.conf
```

then install the plugin through TPM as you normally do.

To actually SEE the status-bar indicator, the plugin needs a placeholder, e.g. `set -g status-right '#{agent_indicator} | %H:%M'` (Atelier adds exactly this inside its own block when it detects the plugin — see below).

### Hook event → state map

The Claude Code hooks the installer wires map agent lifecycle events to states:

| Hook event | State |
|---|---|
| `UserPromptSubmit` | resets (`--state off`) then sets `running` |
| `PermissionRequest` | `needs-input` |
| `Stop` | `done` |

**The hook event is `PermissionRequest`, not `Notification`.** The full hook JSON lives in the plugin's `hooks/claude-hooks.json` — refer to the upstream repo rather than this runbook for the exact template.

## How Atelier integrates it (detect-and-source)

Integration model: **detect-and-source** (operator-consent-respecting). Atelier's generated tmux block (`scripts/tmux_setup.py`, `CONFIG_BLOCK`) carries an `if-shell -b` guard:

```
if-shell -b '[ -d "$HOME/.tmux/plugins/tmux-agent-indicator" ]' " \
    source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux' ; \
    set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖' ; \
    set -g @agent-indicator-indicator-enabled 'on' ; \
    set -g status-right '#{agent_indicator} | %H:%M' \
"
```

- The guard is re-evaluated at config **load** time, so it works even if you install the plugin AFTER Atelier wrote the block.
- When the plugin dir is present, Atelier sources the plugin bootstrap (`-q`, so a missing/renamed file never errors), pins the icon map (the Claude icon is the `claude=` entry inside the single `@agent-indicator-icons` option — there is **no** standalone `@agent-indicator-icon-claude`), enables the indicator, and adds the `#{agent_indicator}` placeholder to `status-right`.
- When the plugin dir is absent, the whole branch is a harmless no-op — the Layer-1 composite render is the fallback.

The Layer-1 composite `pane-border-format` is set **unconditionally, outside the guard**. It is the zero-dependency fallback when the plugin is absent, and it keeps carrying the wave/role label even when the plugin is present (the plugin styles window-scoped border *styles* and the status bar; it does not own `pane-border-format`).

Atelier never runs `curl`, never runs `install.sh`, and never mutates `~/.claude/settings.json` or `~/.tmux.conf` (beyond the single `source-file` include line, taken with a timestamped backup). The integration is purely additive tmux directives inside Atelier's own marker-wrapped block.

## `allow-passthrough` is NOT required

Issue #79's original premise assumed this plugin needs `allow-passthrough` on. **That is wrong.** The plugin has zero occurrences of `allow-passthrough` anywhere in its source. It drives state via Claude Code **hooks** plus `tmux set-option` / `tmux set-hook` — not terminal escape passthrough. Do **not** set `allow-passthrough` for this; Atelier does not, and you should not need to either.

## Trade-off: composite render vs the plugin

| | Atelier composite render (Layer 1, fallback) | tmux-agent-indicator plugin (Layer 2) |
|---|---|---|
| States | 2 (idle / busy, via the OSC-2 glyph in `pane_title`) | 3 (running / needs-input / done) |
| Signal source | CC's OSC-2 pane-title glyph + Atelier's `@desired_title` label | CC's official hooks → `agent-state.sh` |
| Wave/role label | yes (`@desired_title`) | no (Atelier keeps it via the unconditional border render) |
| Dependencies | none — built into Atelier's block | operator must install the plugin + its hooks |
| Border scope | per-pane via `pane-border-format` | window-scoped border *styles* (see caveat) |

**Border caveat (verbatim upstream):** tmux border coloring is window-scoped (`pane-active-border-style` / `pane-border-style`); tmux cannot set a fully independent border color for one arbitrary NON-active pane. The plugin works within that constraint.

**Coexistence is validated here only at the config-composition level** — Atelier's `pane-border-format` and the plugin's status-bar/border-style integration compose by *different* mechanisms, so they are plausibly compatible. Upstream does not document coexistence with an external `pane-border-format` owner, so the live combined render is **NEEDS-TESTING**: smoke-test it on your machine after installing the plugin before relying on it in a real run.

## Related

- `scripts/tmux_setup.py` — `CONFIG_BLOCK`, where the composite render + the detect-and-source guard live.
- `scripts/tmux_layout.py` — `set_pane_title` / `set_pane_titles` / `apply_layout(labels=...)`, the `@desired_title` writers that populate the wave/role label.
- atelier#63 (tmux config writer + layout + pane-state fold-in) and atelier#79 (this composite render + detect-and-source port).
- Upstream: [`accessd/tmux-agent-indicator`](https://github.com/accessd/tmux-agent-indicator) (MIT; tmux 3.0+, bash 4+).
