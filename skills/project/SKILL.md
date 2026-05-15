---
name: atelier-project
description: Use to create, read, update, delete, list, or search Atelier projects.
---

# project

Manages projects in Atelier. A project is a feature, epic, or new repo moving through the dev workflow.

## Commands

- `project:create` — Create a new project
- `project:read` — Get project details and current phase
- `project:update` — Update project metadata
- `project:delete` — Remove a project
- `project:list` — List projects (optionally filter by phase)
- `project:search` — Filter projects by name or keyword

## Procedure

### project:create
1. Ask: "Project name?" / "Description?" / "Repo (optional)?" / "Your agent ID (created_by)?"
2. Run: `python atelier/scripts/projects.py create "<name>" "<description>" "<created_by>" [--repo "<repo>"]`
3. Confirm: "Project created: [name] (id: [id], phase: design:in-progress)"

### project:read
1. Ask: "Project ID?"
2. Run: `python atelier/scripts/projects.py get <id>`
3. Display all fields including current phase.

### project:update
1. Ask: "Project ID?" and "What to update?"
2. Run: `python atelier/scripts/projects.py update <id> [--name "..."] [--description "..."] [--phase "..."] [--repo "..."]`
3. Confirm: "Project updated."

### project:delete
1. Ask: "Project ID? All associated documents and tasks will be affected."
2. Run: `python atelier/scripts/projects.py delete <id>`
3. Confirm: "Project deleted."

### project:list
1. Ask: "Filter by phase? (leave blank for all)"
2. Run: `python atelier/scripts/projects.py list [--phase "<phase>"]`
3. Display results as a table: id | name | phase | repo

### project:search
1. Ask: "Search query?"
2. Run: `python atelier/scripts/projects.py search "<query>"`
3. Display matching projects as a table.

## Hard rules
- Never update `phase` directly via `project:update` during a dev workflow — phase transitions are managed by `dev:*` commands. Direct phase updates are only for corrections.
