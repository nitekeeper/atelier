---
description: Use to create, read, update, delete, list, or search agent roles in Atelier.
---

# role

Manages roles in Atelier. Roles define the type and responsibility of agents.

## Commands

- `internal/role/SKILL.md` (`create`) — Register a new role
- `internal/role/SKILL.md` (`read`) — Get role details
- `internal/role/SKILL.md` (`update`) — Update role name or description
- `internal/role/SKILL.md` (`delete`) — Remove a role
- `internal/role/SKILL.md` (`list`) — List all roles
- `internal/role/SKILL.md` (`search`) — Filter roles by name or keyword

## Procedure

### role:create
1. Ask: "Role name?" and "Role description?"
2. Run: `python atelier/scripts/roles.py create "<name>" "<description>"`
3. Confirm: "Role created: [name] (id: [id])"

### role:read
1. Ask: "Role ID?"
2. Run: `python atelier/scripts/roles.py get <id>`
3. Display all fields.

### role:update
1. Ask: "Role ID?" and "What to update (name / description)?"
2. Run: `python atelier/scripts/roles.py update <id> [--name "<name>"] [--description "<description>"]`
3. Confirm: "Role updated."

### role:delete
1. Ask: "Role ID? This cannot be undone."
2. Run: `python atelier/scripts/roles.py delete <id>`
3. Confirm: "Role deleted."

### role:list
1. Run: `python atelier/scripts/roles.py list`
2. Display results as a table: id | name | description

### role:search
1. Ask: "Search query?"
2. Run: `python atelier/scripts/roles.py search "<query>"`
3. Display matching roles as a table.

## Hard rules
- Never delete a role that has agents assigned to it — the DB will reject it (FK constraint). Inform the user and list the affected agents first.
