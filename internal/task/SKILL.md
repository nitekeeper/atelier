---
description: Use to create, assign, claim, update, complete, list, or search tasks in the current Atelier project.
---

# task

Manages tasks in Atelier. Tasks are created from the TDD red phase and assigned by the coordinator.

## Commands

- `internal/task/SKILL.md` (`create`) — Create a new task
- `internal/task/SKILL.md` (`assign`) — Assign a task to an agent
- `internal/task/SKILL.md` (`claim`) — Worker marks an assigned task as in-progress
- `internal/task/SKILL.md` (`update`) — Write progress notes or update fields
- `internal/task/SKILL.md` (`complete`) — Mark a task complete
- `internal/task/SKILL.md` (`list`) — Query tasks by status, agent, or project
- `internal/task/SKILL.md` (`search`) — Search tasks by any field

## Procedure

### task:create
1. Ask: "Project ID?" / "Task title (imperative verb phrase)?" / "Description?" / "Priority (0–10)?" / "Your agent ID?"
2. Run: `python3 atelier/scripts/tasks.py create <project_id> "<title>" "<created_by>" [--description "..."] [--priority N]`
3. Confirm: "Task created: [title] (id: [id], status: pending)"

### task:assign
1. Ask: "Task ID?" / "Agent ID to assign?"
2. Run: `python3 atelier/scripts/tasks.py assign <task_id> <agent_id>`
3. Confirm: "Task [id] assigned to [agent_id] (status: assigned)"

### task:claim
1. Ask: "Task ID?" / "Your agent ID?"
2. Run: `python3 atelier/scripts/tasks.py claim <task_id> <agent_id>`
3. Confirm: "Task [id] claimed (status: in-progress)"

### task:update
1. Ask: "Task ID?" / "What to update (notes / title / description / priority)?"
2. Run: `python3 atelier/scripts/tasks.py update <task_id> [--notes "..."] [--title "..."] [--description "..."] [--priority N]`
3. Confirm: "Task updated."

### task:complete
1. Ask: "Task ID?"
2. Run: `python3 atelier/scripts/tasks.py complete <task_id>`
3. Confirm: "Task [id] marked complete."

### task:list
1. Ask: "Filter by status? Agent ID? Project ID? (all optional)"
2. Run: `python3 atelier/scripts/tasks.py list [--status "..."] [--assigned_to "..."] [--project_id N]`
3. Display results as a table: id | title | status | assigned_to | priority

### task:search
1. Ask: "Search query?" / "Filter by status or agent? (optional)"
2. Run: `python3 atelier/scripts/tasks.py search "<query>" [--status "..."] [--assigned_to "..."]`
3. Display matching tasks as a table.

## Hard rules
- Tasks cannot be assigned until the project's implementation plan is approved (phase >= plan:approved). Refuse and state the current phase if the gate is not met.
- Only the assigned agent can claim a task. Refuse if agent IDs do not match.
