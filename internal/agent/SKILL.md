---
description: Use to create, read, update, delete, list, or search agents (human or AI participants) in Atelier.
---

# agent

Manages agents in Atelier. Agents are human or AI participants with a role and a profile.

## Commands

- `internal/agent/SKILL.md` (`create`) — Register a new agent
- `internal/agent/SKILL.md` (`read`) — Get agent details
- `internal/agent/SKILL.md` (`update`) — Update agent name, role, or profile
- `internal/agent/SKILL.md` (`delete`) — Remove an agent
- `internal/agent/SKILL.md` (`list`) — List all agents (optionally filter by role)
- `internal/agent/SKILL.md` (`search`) — Filter agents by name, role, or keyword

## Procedure

### agent:create
1. Ask: "Agent ID (e.g. dev-1)?" / "Name?" / "Role ID?" / "Profile (detailed description of this agent's background and capabilities)?"
2. Run: `python atelier/scripts/agents.py create "<id>" "<name>" <role_id> "<profile>"`
3. Confirm: "Agent registered: [name] ([id])"

### agent:read
1. Ask: "Agent ID?"
2. Run: `python atelier/scripts/agents.py get <id>`
3. Display all fields.

### agent:update
1. Ask: "Agent ID?" and "What to update?"
2. Run: `python atelier/scripts/agents.py update <id> [--name "..."] [--role_id N] [--profile "..."]`
3. Confirm: "Agent updated."

### agent:delete
1. Ask: "Agent ID? This cannot be undone."
2. Run: `python atelier/scripts/agents.py delete <id>`
3. Confirm: "Agent removed."

### agent:list
1. Ask: "Filter by role ID? (leave blank for all)"
2. Run: `python atelier/scripts/agents.py list [--role_id N]`
3. Display results as a table: id | name | role_id | profile (truncated)

### agent:search
1. Ask: "Search query?" and "Filter by role ID? (optional)"
2. Run: `python atelier/scripts/agents.py search "<query>" [--role_id N]`
3. Display matching agents as a table.

## Hard rules
- Profile must be substantive — at least 2 sentences describing background and capabilities. Reject one-word profiles.
