---
description: Use to join or leave a tmux agent desk (pane) within a room.
user-invocable: false
---

# agent-desk

Manages agent desks (tmux panes) within a room. Each pane is one agent's desk running a Claude Code CLI session.

## Commands

- `agent:join` — Spawn a Claude Code CLI session for an agent in a new pane
- `agent:leave` — Close an agent's pane

## Procedure

### agent:join
1. Ask: "Workspace name?" / "Room name?" / "Agent ID (from the agents registry, e.g. 'dev-1')?"
2. Check: verify the agent exists — `python atelier/scripts/agents.py get <agent_id>`
3. Run: `python atelier/scripts/workspace.py agent:join <workspace> <room_name> <agent_id>`
4. Confirm: "Agent '[agent_id]' is now at their desk in room '[room_name]'. Claude Code launched in pane [pane_id]."
5. Note the pane ID for future `agent:leave` calls.

### agent:leave
1. Ask: "Workspace name?" / "Room name?" / "Pane ID (e.g. %3)?"
2. Run: `python atelier/scripts/workspace.py agent:leave <workspace> <room_name> <pane_id>`
3. Confirm: "Agent's desk (pane [pane_id]) closed."

## Hard rules
- Always verify the agent exists in the Atelier registry before creating their desk.
- Maximum 3–5 agents per room. Warn at 4; refuse at 6.
- Record the pane ID when an agent joins — it is the only way to identify the pane for `agent:leave`.
