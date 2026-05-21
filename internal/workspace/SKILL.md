---
description: Use to create, join, list, or leave tmux workspaces for multi-agent team sessions.
---

# workspace

Manages tmux workspaces. Each workspace is a tmux session with Claude Code agent teams enabled.

## Commands

- `internal/workspace/SKILL.md` (`create`) — Create a new workspace (tmux session) with a main room
- `internal/workspace/SKILL.md` (`join`) — Attach to an existing workspace
- `internal/workspace/SKILL.md` (`list`) — List all active workspaces
- `internal/workspace/SKILL.md` (`leave`) — Detach from the current workspace

## Procedure

### workspace:create
1. Ask: "Workspace name (use project name, e.g. 'auth-service')?" / "Project root path?"
2. Run: `python3 atelier/scripts/workspace.py workspace:create <name> --root <path>`
3. Confirm: "Workspace '[name]' created. Main room is ready. You are now in the workspace."

### workspace:list
1. Run: `python3 atelier/scripts/workspace.py workspace:list`
2. Display the list of active workspaces.

### workspace:join
1. Ask: "Workspace name?"
2. Run: `python3 atelier/scripts/workspace.py workspace:join <name>`
3. Confirm: "Joined workspace '[name]'."

### workspace:leave
1. Ask: "Workspace name?"
2. Run: `python3 atelier/scripts/workspace.py workspace:leave <name>`
3. Confirm: "Left workspace '[name]'."

## Hard rules
- All sessions start from the project root so all agents share file context.
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is always set — do not create workspaces without it.
- Maximum 3–5 agents per workspace. Warn before the user adds a 4th agent; refuse a 6th.
